"""The feeder's runtime: a GLib main loop driving logic.py's pure state
machine against the V4L2 adapter (v4l2.py), the PipeWire input
(pwinput.py) and the filter child (filterlink.py). See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Start-up and Running, and
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
feeder".

Every camera frame goes through the child: pwinput's appsink callback
only calls `link.submit()`, and only the child's outputs reach `latest`.
State changes only ever happen on the main thread: FilterLink's callbacks
run on its own threads, under its delivery lock, so they only marshal:
`on_output` puts into `latest` (whose lock is never held around a link
call), the others `GLib.idle_add` onto the main thread. `Runner._tick()`
is the one place an output reaches `on_frame()`.
"""
import os
import signal
import sys
import time


def ignore_user_signals() -> None:
    """SIGUSR1/2 (pause/resume) are SIG_IGN from here until _run() installs
    the real GLib handlers, so a click landing anywhere before that is
    dropped instead of hitting the default (terminating) disposition.
    Still called again in main() and after the loop exits (see _run())."""
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    signal.signal(signal.SIGUSR2, signal.SIG_IGN)


# Installed here, before `import gi` below, rather than only at the top of
# main(): the console script imports this module, and gi's imports —
# loading the Gst/GLibUnix typelibs — are an import-time side effect that
# can take hundreds of ms. Left at the default disposition for that window,
# a tray click (systemctl --user kill -s SIGUSR1/2) would kill a
# (re)starting feeder before it ever reaches main().
ignore_user_signals()

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix  # noqa: E402

from ffsb_camera.filter.locate import child_argv  # noqa: E402
from ffsb_camera.filterlink import FilterLink  # noqa: E402
from ffsb_camera.frames import render_bars  # noqa: E402
from ffsb_camera.logic import (  # noqa: E402
    FLUSH_FRAMES,
    Act,
    State,
    accepts_frame,
    on_filter_down,
    on_filter_spawn,
    on_filter_status,
    on_frame,
    on_input,
    on_input_error,
    on_pause,
    on_resume,
    on_tick,
    on_usage,
    status,
)
from ffsb_camera.notify import send_status  # noqa: E402
from ffsb_camera.pwinput import CameraInput, LatestFrame  # noqa: E402
from ffsb_camera.v4l2 import Producer  # noqa: E402

DEVICE_DEFAULT = "/dev/video10"
WIDTH, HEIGHT = 1280, 720
TICK_MS = 33
PAUSE_FLAG_NAME = "ffsb-camera-paused"


def log(*parts):
    print("ffsb-camera-feeder:", *parts, file=sys.stderr, flush=True)


def _make_filter_link(on_output, on_status, on_spawn, on_down) -> FilterLink:
    """Production `make_link`: the `ffsb-camera-filter` beside this entry
    point."""
    return FilterLink(child_argv(argv0=sys.argv[0]), on_output, on_status, on_spawn, on_down)


def filter_field(d) -> str:
    """StatusText's `filter=` value for one STATUS from the child: the
    preset id, `*` when modified, or `error`."""
    if not isinstance(d, dict) or d.get("ok") is not True:
        return "error"
    preset = d.get("preset")
    if not isinstance(preset, str) or not preset or any(c.isspace() for c in preset):
        return "error"
    return preset + ("*" if d.get("modified") is True else "")


class Runner:
    """Owns the State and what drives it: usage events off the V4L2 fd
    (`_pump_usage`), the 30 Hz write tick (`_tick`) and the filter child's
    STATUS / spawn / exit events. `make_input(sink, epoch, on_error)` is
    the PipeWire input's factory and `make_link(on_output, on_status,
    on_spawn, on_down)` the filter child's — `pwinput.CameraInput` and
    `FilterLink` in production, fakes in tests — so START_INPUT never has
    to know which."""

    def __init__(self, producer, make_input, bars: bytes, clock, notify=None, flag_path=None,
                 make_link=None):
        self.producer = producer
        self.make_input = make_input
        self.bars = bars
        self.clock = clock
        self.notify = notify if notify is not None else (lambda status: None)
        self.flag_path = flag_path
        self.state = State()
        self.latest = LatestFrame()
        self.input = None
        self.epoch = 0
        self._last_frame = None
        self._bridge_frame = None
        self._filter_field = "down"
        self._bad_size_logged = False
        self._quitting = False
        self.exit_code = 0
        self.loop = None
        make_link = make_link if make_link is not None else _make_filter_link
        self.link = make_link(self._on_filter_output, self._marshal(self._filter_status),
                              self._marshal(self._filter_spawn), self._marshal(self._filter_down))

    def _status_text(self) -> str:
        return f"camera={status(self.state)} filter={self._filter_field}"

    def _set_state(self, new: State, *, filter_field: str | None = None,
                   force_notify: bool = False) -> None:
        """The one place `self.state` (and the StatusText filter field)
        changes (startup()'s flag load is the sole exception: nothing has
        observed the old state yet, so there is nothing to reconcile or
        notify about). Creates/unlinks the pause flag file on a paused
        flip and sends STATUS when the reported StatusText changes. A
        signal always resends, even on a no-op refusal, so a repeated
        click repairs anything the socket lost."""
        old_text = self._status_text()
        if new.paused != self.state.paused:
            self._reconcile_flag(new.paused)
        self.state = new
        if filter_field is not None:
            self._filter_field = filter_field
        new_text = self._status_text()
        if force_notify or new_text != old_text:
            self.notify(new_text)

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
            # Before start(): only sets the hold intent, which the link
            # sends every new child right after RESET, before any FRAME.
            self.link.hold_empty()
        self.producer.configure(WIDTH, HEIGHT, "NV12")
        self.producer.set_timeout(1000)
        self.producer.install_timeout_image(self.bars)
        self.producer.write(self.bars)
        self.producer.subscribe_usage()
        self.link.start()
        # The single first STATUS: a stale loaded flag with nobody
        # streaming is corrected by the very next usage event, which goes
        # through _set_state and notifies again if it changes.
        self.notify(self._status_text())

    def _new_epoch(self) -> None:
        """Once per START_INPUT and STOP_INPUT: from here on the link
        drops every frame and output of the previous input."""
        self.epoch += 1
        self.link.reset(self.epoch)

    def handle(self, actions) -> None:
        for act in actions:
            if act is Act.START_INPUT:
                if self.input is None:
                    self._new_epoch()
                    self.input = self.make_input(self.link.submit, self.epoch, self._handle_input_error)
                    self.input.start()
            elif act is Act.STOP_INPUT:
                if self.input is not None:
                    self.input.stop()
                    self.input = None
                # A frame from before stop() would otherwise reach the
                # child, sit in `latest`/`_last_frame` and get WRITE_REAL'd
                # on a later tick if usage returns within the freshness
                # window — possibly while the lane is locked. The new
                # epoch has the link drop the stopped input's frames
                # (one still in flight on its streaming thread: the
                # state change to NULL runs later, on the worker) and
                # every output for them; once reset() returns none is
                # delivered, so draining `latest` after it is final.
                self._new_epoch()
                self.latest.take()
                self._last_frame = None
                self._bridge_frame = None
            elif act is Act.REASSERT_TIMEOUT:
                self.producer.set_timeout(1000)
            elif act is Act.WRITE_REAL:
                if self._last_frame is not None:
                    self.producer.write(self._last_frame)
            elif act is Act.WRITE_BARS:
                self.producer.write(self.bars)
            elif act is Act.WRITE_BRIDGE:
                self.producer.write(self._bridge_frame if self._bridge_frame is not None else self.bars)
            elif act is Act.HOLD_GRAB:
                # The last output accepted before the press: what the
                # tick bridges with until the child's held output.
                self._bridge_frame = self._last_frame
                self.link.hold_grab()
            elif act is Act.HOLD_EMPTY:
                self.link.hold_empty()
            elif act is Act.RELEASE:
                self._bridge_frame = None
                self.link.release()
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
        self._set_state(on_input(self.state, self.link.last_input_t))
        frame = self.latest.take()
        if frame is not None:
            data, t, held = frame
            # A filtered frame is written as-is, so one that isn't exactly
            # a bars-sized NV12 frame (stride padding, a caps surprise, a
            # child bug) would land in the ring garbled. Drop it; with no
            # fresh frame the tick writes bars instead.
            if len(data) != len(self.bars):
                if not self._bad_size_logged:
                    log(f"dropping real frames of {len(data)} bytes, want {len(self.bars)}")
                    self._bad_size_logged = True
            elif accepts_frame(self.state, held):
                # Only an accepted output may become _last_frame: while
                # paused an unheld one is newer than the press.
                self._last_frame = data
                self._set_state(on_frame(self.state, t, held))
        new, acts = on_tick(self.state, self.clock())
        self._set_state(new)
        self.handle(acts)

    # --- filter child callbacks -------------------------------------------
    #
    # Run on FilterLink's threads under its delivery lock, which reset()
    # and stop() take from this thread: marshal only, never block.

    def _on_filter_output(self, frame: bytes, input_t: float, held: bool) -> None:
        self.latest.put(frame, input_t, held)

    def _marshal(self, fn):
        def callback(*args):
            GLib.idle_add(self._run_marshalled, fn, args)
        return callback

    def _run_marshalled(self, fn, args) -> bool:
        """On the main thread. Not fatal on failure, like a user signal:
        a lost STATUS is repaired by the child's next one."""
        try:
            fn(*args)
        except Exception as e:
            log("filter event handling failed:", e)
        return GLib.SOURCE_REMOVE

    def _filter_status(self, d) -> None:
        field = filter_field(d)
        self._set_state(on_filter_status(self.state, field != "error"), filter_field=field)

    def _filter_spawn(self) -> None:
        new, acts = on_filter_spawn(self.state)
        self._set_state(new)
        self.handle(acts)

    def _filter_down(self) -> None:
        self._set_state(on_filter_down(self.state), filter_field="down")

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
        notify socket lost, not just confirm a change.

        Wrapped in try/except like _on_tick_timer/_on_usage_ready: PyGObject
        removes a raising callback's GLib source and GLib then restores the
        signal's default disposition, so an uncaught exception here (e.g. a
        notify that raises) would silently drop SIGUSR1/2 handling and leave
        the next tray click to SIG_DFL (kills the process). A failed notify
        is not a fatal failure, unlike a tick or usage-watch failure — log
        and keep going."""
        try:
            if signum == signal.SIGUSR1:
                new, acts, refusal = on_pause(self.state, self.clock())
                if refusal is not None:
                    log(f"pause refused: {refusal.name.lower()}")
            else:
                new, acts = on_resume(self.state)
            self.handle(acts)
            self._set_state(new, force_notify=True)
        except Exception as e:
            log("user signal handling failed:", e)
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
        try:
            self.link.stop()
        except Exception as e:
            log("filter child stop on exit failed:", e)
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
        # GLib restores SIG_DFL once its own handler is removed, reopening
        # the start-up window this closed: a click between loop exit and
        # process exit must still be dropped, not kill the (already
        # shutting down) process.
        ignore_user_signals()
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


def _make_notify(send=send_status):
    """Wraps a STATUS sender (send_status for the real NOTIFY_SOCKET; a
    fake in tests) for use as Runner's `notify` — called only when the
    reported status changes, or on every pause/resume signal, never once
    per tick. Logs a failed send once per distinct status value, not once
    per failed send, so a socket that is simply gone doesn't spam the log
    on every repeated click. Any successful send clears every value's
    dedup: a working socket means whatever outage caused the logged
    failures is over, so the next failure (for any status) is worth
    logging again."""
    failed = set()

    def notify(status: str) -> None:
        if send(status):
            failed.clear()
            return
        if status not in failed:
            log(f"notify: failed to send STATUS={status}")
            failed.add(status)

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
            make_input=lambda sink, epoch, on_error: CameraInput(sink, epoch, on_error),
            bars=bars,
            clock=time.monotonic,
            notify=_make_notify(),
            flag_path=flag_path,
            make_link=_make_filter_link,
        )
        return runner.main(argv)
    except Exception as e:
        log("start-up failed:", e)
        return 1
    finally:
        producer.close()


if __name__ == "__main__":
    sys.exit(main())
