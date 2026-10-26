"""The feeder's runtime: a GLib main loop driving logic.py's pure state
machine against the V4L2 adapter (v4l2.py) and the PipeWire input
(pwinput.py). See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Start-up and Running.

State changes only ever happen on the main thread: pwinput's appsink
callback only calls `latest.put()`; `Runner._tick()` is the one place a
real frame reaches `on_frame()`.
"""
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix  # noqa: E402

from ffsb_camera.frames import render_bars
from ffsb_camera.logic import (
    FLUSH_FRAMES,
    Act,
    State,
    on_frame,
    on_input_error,
    on_pause,
    on_resume,
    on_tick,
    on_usage,
    status,
)
from ffsb_camera.notify import send_status
from ffsb_camera.pwinput import CameraInput, LatestFrame
from ffsb_camera.v4l2 import Producer

DEVICE_DEFAULT = "/dev/video10"
WIDTH, HEIGHT = 1280, 720
TICK_MS = 33
PAUSE_FLAG_NAME = "ffsb-camera-paused"


def log(*parts):
    print("ffsb-camera-feeder:", *parts, file=sys.stderr, flush=True)


def ignore_user_signals() -> None:
    """SIGUSR1/2 (pause/resume) are SIG_IGN from the top of main() until
    _run() installs the real GLib handlers, so a click during start-up is
    dropped instead of hitting the default disposition (which kills the
    process)."""
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    signal.signal(signal.SIGUSR2, signal.SIG_IGN)


class Runner:
    """Owns the State and the two things that drive it: usage events off
    the V4L2 fd (`_pump_usage`) and the 30 Hz write tick (`_tick`).
    `make_input(latest, on_error)` is the PipeWire input's factory —
    `pwinput.CameraInput` in production, a fake in tests — so START_INPUT
    never has to know which."""

    def __init__(self, producer, make_input, bars: bytes, clock, notify=None, flag_path=None):
        self.producer = producer
        self.make_input = make_input
        self.bars = bars
        self.clock = clock
        self.notify = notify if notify is not None else (lambda status: None)
        self.flag_path = flag_path
        self.state = State()
        self.latest = LatestFrame()
        self.input = None
        self._last_frame = None
        self._frozen_frame = None
        self._bad_size_logged = False
        self._quitting = False
        self.exit_code = 0
        self.loop = None

    def _set_state(self, new: State, *, force_notify: bool = False) -> None:
        """The one place `self.state` changes (startup()'s flag load is
        the sole exception: nothing has observed the old state yet, so
        there is nothing to reconcile or notify about). Creates/unlinks
        the pause flag file on a paused flip and sends STATUS when the
        reported status changes. A signal always resends, even on a
        no-op refusal, so a repeated click repairs anything the socket
        lost."""
        old_status = status(self.state)
        if new.paused != self.state.paused:
            self._reconcile_flag(new.paused)
        self.state = new
        new_status = status(new)
        if force_notify or new_status != old_status:
            self.notify(new_status)

    def _reconcile_flag(self, paused: bool) -> None:
        if self.flag_path is None:
            return
        try:
            if paused:
                open(self.flag_path, "w").close()
            else:
                os.remove(self.flag_path)
        except OSError as e:
            log("pause flag file:", e)

    def startup(self) -> None:
        if self.flag_path is not None and os.path.exists(self.flag_path):
            # Loaded directly: nothing has observed the default state yet,
            # so there is no prior status to compare against or notify.
            self.state = State(paused=True)
        self.producer.configure(WIDTH, HEIGHT, "NV12")
        self.producer.set_timeout(1000)
        self.producer.install_timeout_image(self.bars)
        self.producer.write(self.bars)
        self.producer.subscribe_usage()
        # The single first STATUS: a stale loaded flag with nobody
        # streaming is corrected by the very next usage event, which goes
        # through _set_state and notifies again if it changes.
        self.notify(status(self.state))

    def handle(self, actions) -> None:
        for act in actions:
            if act is Act.START_INPUT:
                if self.input is None:
                    self.input = self.make_input(self.latest, self._handle_input_error)
                    self.input.start()
            elif act is Act.STOP_INPUT:
                if self.input is not None:
                    self.input.stop()
                    self.input = None
                # A frame that landed before stop() would otherwise sit
                # in `latest`/`_last_frame` and get WRITE_REAL'd on a
                # later tick if usage returns within the freshness
                # window — possibly while the lane is locked. Drop it,
                # and swap in a fresh holder: the stopped input keeps the
                # old one, so a put still in flight on its streaming
                # thread (the state change to NULL runs later, on the
                # worker) lands in an orphan, not in a later tick.
                self.latest = LatestFrame()
                self._last_frame = None
                self._frozen_frame = None
            elif act is Act.REASSERT_TIMEOUT:
                self.producer.set_timeout(1000)
            elif act is Act.WRITE_REAL:
                if self._last_frame is not None:
                    self.producer.write(self._last_frame)
            elif act is Act.WRITE_BARS:
                self.producer.write(self.bars)
            elif act is Act.WRITE_FROZEN:
                self.producer.write(self._frozen_frame if self._frozen_frame is not None else self.bars)
            elif act is Act.FREEZE:
                self._frozen_frame = self._last_frame
            elif act is Act.THAW:
                self._frozen_frame = None
            elif act is Act.FLUSH:
                for _ in range(FLUSH_FRAMES):
                    self.producer.write(self.bars)
            elif act is Act.EXIT_FAILURE:
                self._quit(1)
            else:
                raise AssertionError(f"unhandled action: {act}")

    def _pump_usage(self) -> None:
        """The event queue holds only the latest value (gate 1: usage is
        a level, not a count of edges), so loop read_usage() until it
        returns None and feed on_usage only the last value seen."""
        last = None
        while True:
            value = self.producer.read_usage()
            if value is None:
                break
            last = value
        if last is not None:
            new, acts = on_usage(self.state, last, self.clock())
            self._set_state(new)
            self.handle(acts)

    def _tick(self) -> None:
        frame = self.latest.take()
        if frame is not None:
            data, t = frame
            # A real frame is written as-is, so one that isn't exactly a
            # bars-sized NV12 frame (stride padding, a caps surprise) would
            # land in the ring garbled. Drop it; with no fresh frame the
            # tick writes bars instead.
            if len(data) != len(self.bars):
                if not self._bad_size_logged:
                    log(f"dropping real frames of {len(data)} bytes, want {len(self.bars)}")
                    self._bad_size_logged = True
            else:
                self._last_frame = data
                self._set_state(on_frame(self.state, t))
        new, acts = on_tick(self.state, self.clock())
        self._set_state(new)
        self.handle(acts)

    def _handle_input_error(self, message, overdue=False) -> None:
        log("input error:", message)
        if overdue:
            # A wedged state change, whatever the usage level: the worker
            # is FIFO, so every later start and stop queues behind it. An
            # old input's stop stuck there keeps its pipewiresrc node, and
            # capture.lua's in-use, up for good (LED on, no relock).
            self.handle([Act.EXIT_FAILURE])
            return
        new, acts = on_input_error(self.state)
        self._set_state(new)
        self.handle(acts)

    def _on_user_signal(self, signum) -> bool:
        """SIGUSR1 (pause/freeze) or SIGUSR2 (resume), delivered on the
        main thread between ticks. Always resends STATUS, even when
        on_pause refuses: a repeated click should repair anything the
        notify socket lost, not just confirm a change."""
        if signum == signal.SIGUSR1:
            new, acts, refusal = on_pause(self.state, self.clock())
            if refusal is not None:
                log(f"pause refused: {refusal.name.lower()}")
        else:
            new, acts = on_resume(self.state)
        self.handle(acts)
        self._set_state(new, force_notify=True)
        return GLib.SOURCE_CONTINUE

    def _quit(self, code: int) -> None:
        """Every exit from the loop goes through here. Best effort, since
        it also runs when a write has just failed: flush the ring to bars
        (ring invariant: no real frame left for the next reader while the
        feeder is down) and stop the input, then quit."""
        if self._quitting:
            return
        self._quitting = True
        self.exit_code = code
        try:
            for _ in range(FLUSH_FRAMES):
                self.producer.write(self.bars)
        except Exception as e:
            log("exit flush failed:", e)
        if self.input is not None:
            try:
                self.input.stop()
            except Exception as e:
                log("input stop on exit failed:", e)
            self.input = None
        if self.loop is not None:
            self.loop.quit()

    def _on_signal(self, signum) -> bool:
        log(f"{signal.Signals(signum).name}: flushing and exiting")
        self._quit(0 if signum == signal.SIGTERM else 128 + signum)
        return GLib.SOURCE_CONTINUE  # _run() removes it; _quit() is idempotent

    def main(self, argv=None) -> int:
        try:
            self.startup()
        except Exception as e:
            log("start-up failed:", e)
            return 1
        return self._run()

    def _run(self) -> int:
        self.loop = GLib.MainLoop()
        GLib.io_add_watch(self.producer.fileno(), GLib.IO_PRI, self._on_usage_ready)
        GLib.timeout_add(TICK_MS, self._on_tick_timer)
        signals = [GLibUnix.signal_add(GLib.PRIORITY_HIGH, signum, self._on_signal, signum)
                   for signum in (signal.SIGTERM, signal.SIGINT)]
        # Replaces the SIG_IGN ignore_user_signals() set at start-up.
        signals += [GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_user_signal, signum)
                    for signum in (signal.SIGUSR1, signal.SIGUSR2)]
        self.loop.run()
        for source in signals:
            GLib.source_remove(source)
        return self.exit_code

    def _on_usage_ready(self, fd, condition) -> bool:
        try:
            self._pump_usage()
        except Exception as e:
            self._fail_from_callback("usage watch", e)
            return False
        return True

    def _on_tick_timer(self) -> bool:
        try:
            self._tick()
        except Exception as e:
            self._fail_from_callback("tick", e)
            return False
        return True

    def _fail_from_callback(self, context: str, e: Exception) -> None:
        """PyGObject prints and *removes the source* on an exception out
        of a timeout_add/io_add_watch callback — it does not kill the
        process. Left alone, a raising producer.write() would silently
        stop the 30 Hz tick forever (or read_usage() the usage watch)
        while the process stays up and "active", so systemd's
        Restart=on-failure never fires. Fail loudly instead, same as
        EXIT_FAILURE."""
        log(f"{context} failed:", e)
        self._quit(1)


def _make_notify():
    """Wraps notify.send_status for the real NOTIFY_SOCKET: logs a failed
    send once per distinct status value, not once per failed send, so a
    socket that is simply gone doesn't spam the log every 33 ms. The dedup
    resets the moment a send for that value succeeds."""
    last_failed = None

    def notify(status: str) -> None:
        nonlocal last_failed
        if send_status(status):
            last_failed = None
            return
        if status != last_failed:
            log(f"notify: failed to send STATUS={status}")
            last_failed = status

    return notify


def main(argv=None) -> int:
    ignore_user_signals()
    device = os.environ.get("FFSB_CAMERA_DEVICE", DEVICE_DEFAULT)
    try:
        producer = Producer(device)
    except OSError as e:
        log(f"could not open {device}:", e)
        return 1
    try:
        bars = render_bars(WIDTH, HEIGHT)
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir:
            flag_path = os.path.join(runtime_dir, PAUSE_FLAG_NAME)
        else:
            flag_path = None
            log("XDG_RUNTIME_DIR not set: pause flag file disabled")
        runner = Runner(
            producer,
            make_input=lambda latest, on_error: CameraInput(latest, on_error),
            bars=bars,
            clock=time.monotonic,
            notify=_make_notify(),
            flag_path=flag_path,
        )
        return runner.main(argv)
    except Exception as e:
        log("start-up failed:", e)
        return 1
    finally:
        producer.close()


if __name__ == "__main__":
    sys.exit(main())
