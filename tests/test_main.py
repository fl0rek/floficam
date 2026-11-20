"""The feeder's GLib-driven runtime: start-up order, action effects, and
the two error paths that must exit non-zero so systemd restarts the
service. Fakes only: no real V4L2 device, no real PipeWire, no filter
child (FakeLink stands in for FilterLink). See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Start-up and Running, and
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
feeder".
"""
import os
import signal
import threading
import time

from gi.repository import GLib

from floficam import v4l2
from floficam.logic import DEBOUNCE_S, FLUSH_FRAMES, Act, on_tick, on_usage
from floficam.main import Runner, _make_notify, ignore_user_signals

BARS = b"B" * 10


class FakeProducer:
    """Records every call main.py makes on the V4L2 adapter. `usage_queue`
    feeds read_usage(); configure()/install_timeout_image() can be made
    to raise, simulating the two start-up failures the device can report."""

    def __init__(self, configure_error=None, install_timeout_image_error=None,
                 write_error=None, read_usage_error=None):
        self.calls = []
        self.usage_queue = []
        self._configure_error = configure_error
        self._install_timeout_image_error = install_timeout_image_error
        self._write_error = write_error
        self._read_usage_error = read_usage_error

    def configure(self, width, height, fourcc):
        self.calls.append(("configure", width, height, fourcc))
        if self._configure_error is not None:
            raise self._configure_error

    def set_timeout(self, ms):
        self.calls.append(("set_timeout", ms))

    def install_timeout_image(self, frame):
        self.calls.append(("install_timeout_image", frame))
        if self._install_timeout_image_error is not None:
            raise self._install_timeout_image_error

    def write(self, frame):
        self.calls.append(("write", frame))
        if self._write_error is not None:
            raise self._write_error
        return len(frame)

    def subscribe_usage(self):
        self.calls.append(("subscribe_usage",))

    def read_usage(self):
        if self._read_usage_error is not None:
            raise self._read_usage_error
        return self.usage_queue.pop(0) if self.usage_queue else None

    def fileno(self):
        return self._fileno

    _fileno = -1

    def close(self):
        self.calls.append(("close",))


class FakeInput:
    """Stands in for pwinput.CameraInput. Keeps the `sink`, `epoch` and
    `on_error` it was built with so a test can fire them, the way the
    appsink callback and a real bus ERROR would (the latter marshalled
    onto the main thread first, which is pwinput's job, not Runner's)."""

    def __init__(self, sink, epoch, on_error):
        self.sink = sink
        self.epoch = epoch
        self.on_error = on_error
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def camera(self, frame, t):
        """A camera frame from this input's appsink."""
        self.sink(frame, t, self.epoch)


def make_input_factory():
    """A make_input callable plus a way to reach the FakeInput it built."""
    created = {}

    def make_input(sink, epoch, on_error):
        inp = FakeInput(sink, epoch, on_error)
        created["input"] = inp
        return inp

    return make_input, created


class FakeLink:
    """Stands in for filterlink.FilterLink, built by Runner through
    `make_link`. Records every call; `submit` drops a frame whose epoch
    isn't the last reset's, and tracks `last_input_t`, like the real one.
    The callbacks are the ones Runner handed it, so a test can play the
    child's side (`on_output`, `on_status`) or the supervisor's
    (`on_spawn`, `on_down`) from the test's own thread."""

    def __init__(self, on_output, on_status, on_spawn, on_down):
        self.on_output = on_output
        self.on_status = on_status
        self.on_spawn = on_spawn
        self.on_down = on_down
        self.calls = []
        self.submitted = []
        self.epoch = 0
        self.last_input_t = None

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def reset(self, epoch):
        self.calls.append(("reset", epoch))
        self.epoch = epoch
        self.last_input_t = None

    def submit(self, frame, input_t, epoch):
        if epoch != self.epoch:
            return
        self.submitted.append((frame, input_t, epoch))
        self.last_input_t = input_t

    def hold_grab(self):
        self.calls.append(("hold_grab",))

    def hold_empty(self):
        self.calls.append(("hold_empty",))

    def release(self):
        self.calls.append(("release",))

    def resets(self):
        return [c[1] for c in self.calls if c[0] == "reset"]


def drain_idle():
    """Runs whatever Runner's link callbacks queued with GLib.idle_add."""
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass


def filter_status(runner, preset="none", modified=False, ok=True):
    """The child's STATUS, delivered and marshalled onto the main thread."""
    d = {"preset": preset, "modified": modified, "ok": ok, "holding": "none", "ms_per_frame": 1.0}
    if not ok:
        d["error"] = "bad settings"
    runner.link.on_status(d)
    drain_idle()


def test_startup_order():
    producer = FakeProducer()
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    runner.startup()

    assert producer.calls == [
        ("configure", 1280, 720, "NV12"),
        ("set_timeout", 1000),
        ("install_timeout_image", BARS),
        ("write", BARS),
        ("subscribe_usage",),
    ]
    assert runner.link.calls == [("start",)]


def test_flush_writes_two_bars_then_stops_input():
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    producer.usage_queue = [1]
    runner._pump_usage()
    fake_input = created["input"]
    assert fake_input.started is True

    producer.calls.clear()
    runner.handle([Act.FLUSH, Act.STOP_INPUT])

    assert producer.calls == [("write", BARS)] * FLUSH_FRAMES
    assert fake_input.stopped is True
    assert runner.input is None


def test_input_error_while_streaming_exits_1():
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    producer.usage_queue = [1]
    runner._pump_usage()
    fake_input = created["input"]
    assert fake_input.started is True
    assert runner.exit_code == 0

    # The bus-error callback, fired the way pwinput.CameraInput fires it.
    fake_input.on_error("pipewiresrc: not-negotiated")

    assert runner.exit_code == 1


def test_format_mismatch_exits_1():
    error = v4l2.FormatMismatch(got=(640, 360, 0), want=(1280, 720, 0))
    producer = FakeProducer(configure_error=error)
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    assert runner.main() == 1


def test_timeout_image_lost_exits_1():
    error = v4l2.TimeoutImageLost("REQBUFS failed: fake")
    producer = FakeProducer(install_timeout_image_error=error)
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    assert runner.main() == 1


def test_frame_from_thread_does_not_race():
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    producer.usage_queue = [1]
    runner._pump_usage()
    assert created["input"].started is True

    stop = threading.Event()

    def spam():
        i = 0
        while not stop.is_set():
            runner.latest.put(f"frame{i}".encode(), time.monotonic())
            i += 1

    t = threading.Thread(target=spam)
    t.start()
    try:
        # The main side tears the input down (usage 0, then past the
        # debounce window) while frames keep landing from the thread.
        runner.state, acts = on_usage(runner.state, 0, 0.0)
        runner.handle(acts)
        runner.state, acts = on_tick(runner.state, 10.0)
        runner.handle(acts)
        for _ in range(50):
            runner._tick()
    finally:
        stop.set()
        t.join()

    assert runner.input is None

    # Usage 1 on a torn-down input must still start one, unperturbed by
    # the concurrent latest.put() calls.
    _, acts = on_usage(runner.state, 1, 10.0)
    assert Act.START_INPUT in acts


def test_tick_write_failure_exits_1():
    """PyGObject prints and *drops the source* on an exception out of a
    timeout_add callback — it does not kill the process. Without the
    try/except in _on_tick_timer, a raising write() would silently stop
    the 30 Hz tick forever while the process stays "active", and
    Restart=on-failure would never fire."""
    producer = FakeProducer(write_error=OSError("disk full"))
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    producer.usage_queue = [1]
    runner._pump_usage()  # starts the input; no write yet
    assert runner.exit_code == 0

    # input_up, no real frame yet -> on_tick emits WRITE_BARS -> write() raises.
    result = runner._on_tick_timer()

    assert result is False  # tells GLib not to call it again
    assert runner.exit_code == 1


def test_usage_read_failure_exits_1():
    """Same failure mode as above, for the io_add_watch callback: a
    raising read_usage() (a DQEVENT ioctl failure, say) must not just
    silently drop the usage watch."""
    producer = FakeProducer(read_usage_error=OSError("ioctl failed"))
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    result = runner._on_usage_ready(producer.fileno(), 0)

    assert result is False
    assert runner.exit_code == 1


def test_stop_input_drains_latest_frame():
    """A frame that lands just before STOP_INPUT must not survive it: if
    usage returns within the freshness window, the next WRITE_REAL must
    not resurrect a frame from before the lane was torn down."""
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    producer.usage_queue = [1]
    runner._pump_usage()
    runner.latest.put(b"stale real frame", 0.0)
    runner._last_frame = b"stale real frame"

    runner.handle([Act.STOP_INPUT])

    assert created["input"].stopped is True
    assert runner.latest.take() is None
    assert runner._last_frame is None


def test_late_put_from_stopped_input_dropped_by_epoch():
    """A frame still in flight on a stopped input's streaming thread (its
    NULL runs later, on the worker) must never reach the child or a
    tick, even once the next input is up: it carries the stopped input's
    epoch, which the link no longer accepts."""
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    producer.usage_queue = [1]
    runner._pump_usage()
    stopped = created["input"]

    runner.handle([Act.STOP_INPUT])
    runner.handle([Act.START_INPUT])
    fresh = created["input"]
    assert fresh is not stopped and fresh.epoch != stopped.epoch

    stopped.camera(b"late real frame", 0.0)
    assert runner.link.submitted == []
    runner._tick()
    assert runner._last_frame is None
    assert runner.state.last_real is None
    assert ("write", b"late real frame") not in producer.calls

    fresh.camera(b"new real frame", 0.0)
    assert runner.link.submitted == [(b"new real frame", 0.0, fresh.epoch)]


def test_epoch_bumps_once_per_start_and_stop():
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)

    runner.handle([Act.START_INPUT])
    assert (runner.link.resets(), created["input"].epoch) == ([1], 1)
    runner.handle([Act.START_INPUT])                  # already up: a no-op
    assert runner.link.resets() == [1]
    runner.handle([Act.STOP_INPUT])
    assert runner.link.resets() == [1, 2]
    runner.handle([Act.START_INPUT])
    assert (runner.link.resets(), created["input"].epoch) == ([1, 2, 3], 3)


def test_output_goes_to_latest_with_held_flag():
    producer = FakeProducer()
    runner, _ = started_runner(producer)
    runner.link.on_output(b"F", 1.5, True)
    assert runner.latest.take() == (b"F", 1.5, True)


class FakeLoop:
    """Records quit() into the producer's call log, so a test sees the
    flush writes and the quit in order."""

    def __init__(self, calls):
        self.calls = calls

    def quit(self):
        self.calls.append(("quit",))


class MutableClock:
    """A settable clock for tests that need to move time past FRESH_S or
    DEBOUNCE_S without a real sleep."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def started_runner(producer, notify=None, flag_path=None, clock=None):
    make_input, created = make_input_factory()
    clock = clock if clock is not None else MutableClock()
    runner = Runner(producer, make_input, bars=BARS, clock=clock, notify=notify,
                    flag_path=flag_path, make_link=FakeLink)
    runner.loop = FakeLoop(producer.calls)
    producer.usage_queue = [1]
    runner._pump_usage()
    producer.calls.clear()
    return runner, created["input"]


def frame_through(runner, inp, data, t, held=False):
    """One camera frame into the link, and the child's output for it."""
    inp.camera(data, t)
    runner.link.on_output(data, t, held)


def test_exit_failure_flushes_bars_then_quits():
    """Ring invariant on the way out: the newest slot must not stay the
    last real frame while the feeder is down."""
    producer = FakeProducer()
    runner, fake_input = started_runner(producer)
    runner._last_frame = b"real"

    runner.handle([Act.EXIT_FAILURE])

    assert producer.calls == [("write", BARS)] * FLUSH_FRAMES + [("quit",)]
    assert fake_input.stopped is True
    assert runner.link.calls[-1] == ("stop",)
    assert runner.exit_code == 1


def test_failing_callback_flushes_bars_then_quits():
    producer = FakeProducer()
    runner, _ = started_runner(producer)
    producer._read_usage_error = OSError("ioctl failed")

    assert runner._on_usage_ready(producer.fileno(), 0) is False

    assert producer.calls == [("write", BARS)] * FLUSH_FRAMES + [("quit",)]
    assert runner.exit_code == 1


def test_raising_flush_write_still_quits():
    producer = FakeProducer()
    runner, fake_input = started_runner(producer)
    producer._write_error = OSError("ENODEV")

    runner.handle([Act.EXIT_FAILURE])

    assert producer.calls[-1] == ("quit",)
    assert fake_input.stopped is True
    assert runner.exit_code == 1


def test_sigterm_handler_flushes_and_exits_0():
    producer = FakeProducer()
    runner, fake_input = started_runner(producer)

    runner._on_signal(signal.SIGTERM)

    assert producer.calls == [("write", BARS)] * FLUSH_FRAMES + [("quit",)]
    assert fake_input.stopped is True
    assert runner.exit_code == 0


def test_sigterm_reaches_the_running_loop():
    """The handler is actually installed by _run(): a real SIGTERM ends
    the real loop with bars flushed and exit code 0."""
    producer = FakeProducer()
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink)
    r, w = os.pipe()
    producer._fileno = r
    try:
        GLib.timeout_add(50, lambda: os.kill(os.getpid(), signal.SIGTERM) and False)
        assert runner._run() == 0
    finally:
        os.close(r)
        os.close(w)

    assert producer.calls == [("write", BARS)] * FLUSH_FRAMES


def test_overdue_report_exits_even_with_usage_0():
    """A wedged stop job (old input, usage already 0) keeps its node and
    capture.lua's in-use up; the worker is FIFO, so nothing after it runs
    either. Fatal at any usage level, unlike an ordinary input error."""
    producer = FakeProducer()
    runner, fake_input = started_runner(producer)
    runner.state, acts = on_usage(runner.state, 0, 0.0)
    runner.handle(acts)

    fake_input.on_error("pipeline stop still unfinished after 10 s", overdue=True)

    assert runner.exit_code == 1
    assert producer.calls[-1] == ("quit",)


def test_ordinary_error_with_usage_0_does_not_exit():
    producer = FakeProducer()
    runner, fake_input = started_runner(producer)
    runner.state, acts = on_usage(runner.state, 0, 0.0)
    runner.handle(acts)

    fake_input.on_error("pipewiresrc reached EOS")

    assert runner.exit_code == 0
    assert ("quit",) not in producer.calls


def test_wrong_size_real_frame_is_dropped():
    """Real frames are written raw; one that isn't exactly frame-sized
    (stride padding) would land garbled. The tick writes bars instead."""
    producer = FakeProducer()
    runner, _ = started_runner(producer)

    runner.latest.put(b"R" * (len(BARS) + 2), 0.0)
    runner._tick()

    assert runner._last_frame is None
    assert runner.state.last_real is None
    assert producer.calls == [("write", BARS)]

    good = b"R" * len(BARS)
    runner.latest.put(good, 0.0)
    runner._tick()
    assert producer.calls[-1] == ("write", good)


A = b"A" * len(BARS)
C = b"C" * len(BARS)
D = b"D" * len(BARS)
H = b"H" * len(BARS)


def test_pause_writes_held_output_never_newer_unheld():
    producer = FakeProducer()
    notes = []
    runner, inp = started_runner(producer, notify=notes.append)
    filter_status(runner)

    frame_through(runner, inp, A, 0.0)
    runner._tick()

    assert runner._on_user_signal(signal.SIGUSR1) == GLib.SOURCE_CONTINUE
    assert runner.link.calls[-1] == ("hold_grab",)

    frame_through(runner, inp, C, 0.0)                # in flight at the press
    runner._tick()
    assert producer.calls[-1] == ("write", A)          # the bridge

    runner.link.on_output(H, 0.0, True)               # the child's grab
    runner._tick()
    assert producer.calls[-1] == ("write", H)

    frame_through(runner, inp, C, 0.0)                # the child ignoring the hold
    runner._tick()
    assert producer.calls[-1] == ("write", H)
    assert ("write", C) not in producer.calls
    assert notes[-1] == "camera=frozen filter=none"


def test_frames_stopping_while_paused_writes_bars():
    """Lock beats pause: once there is no fresh frame, a tick writes bars
    even while frozen."""
    producer = FakeProducer()
    clock = MutableClock()
    runner, inp = started_runner(producer, clock=clock)
    filter_status(runner)

    frame_through(runner, inp, A, 0.0)
    runner._tick()
    runner._on_user_signal(signal.SIGUSR1)
    runner.link.on_output(H, 0.0, True)
    runner._tick()
    assert producer.calls[-1] == ("write", H)

    clock.t += 10.0  # well past FRESH_S, no new frame
    runner._tick()

    assert producer.calls[-1] == ("write", BARS)


def test_bridge_writes_pre_press_frame():
    """No test-card blip at the press: until the child's held output
    arrives, the ticks repeat the last frame written before it."""
    producer = FakeProducer()
    clock = MutableClock()
    runner, inp = started_runner(producer, clock=clock)
    filter_status(runner)

    frame_through(runner, inp, A, 0.0)
    runner._tick()
    assert producer.calls[-1] == ("write", A)
    runner._on_user_signal(signal.SIGUSR1)

    for i in range(3):
        clock.t += 0.033
        frame_through(runner, inp, C, clock.t)        # newer than the press: never written
        runner._tick()
        assert producer.calls[-1] == ("write", A)

    runner.link.on_output(H, clock.t, True)
    runner._tick()
    assert producer.calls[-1] == ("write", H)
    assert ("write", C) not in producer.calls


def test_resume_and_stop_input_drop_bridge_frame():
    producer = FakeProducer()
    notes = []
    runner, inp = started_runner(producer, notify=notes.append)
    filter_status(runner)

    frame_through(runner, inp, A, 0.0)
    runner._tick()
    runner._on_user_signal(signal.SIGUSR1)
    assert runner._bridge_frame == A

    runner._on_user_signal(signal.SIGUSR2)
    assert runner.link.calls[-1] == ("release",)
    assert runner._bridge_frame is None
    assert notes[-1] == "camera=live filter=none"

    frame_through(runner, inp, D, 0.0)
    runner._tick()
    assert producer.calls[-1] == ("write", D)

    runner._on_user_signal(signal.SIGUSR1)
    assert runner._bridge_frame == D
    runner.handle([Act.STOP_INPUT])
    assert runner._bridge_frame is None


def test_flag_file_follows_paused(tmp_path):
    producer = FakeProducer()
    notes = []
    flag_path = str(tmp_path / "floficam-paused")
    clock = MutableClock()
    runner, inp = started_runner(producer, notify=notes.append, flag_path=flag_path, clock=clock)
    filter_status(runner)

    frame_through(runner, inp, A, 0.0)
    runner._tick()
    runner._on_user_signal(signal.SIGUSR1)
    assert os.path.exists(flag_path)

    runner._on_user_signal(signal.SIGUSR2)
    assert not os.path.exists(flag_path)

    runner._on_user_signal(signal.SIGUSR1)
    assert os.path.exists(flag_path)

    runner.state, acts = on_usage(runner.state, 0, clock())
    runner.handle(acts)
    clock.t += DEBOUNCE_S
    runner._tick()

    assert not os.path.exists(flag_path)
    assert runner.link.calls[-1] == ("release",)
    assert notes[-1] == "camera=live filter=none"


def test_startup_with_flag_is_paused_card(tmp_path):
    producer = FakeProducer()
    notes = []
    flag_path = str(tmp_path / "floficam-paused")
    open(flag_path, "w").close()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0,
                    notify=notes.append, flag_path=flag_path, make_link=FakeLink)
    runner.loop = FakeLoop(producer.calls)

    runner.startup()
    assert notes[-1] == "camera=paused-card filter=down"
    # The hold intent is set before the child is ever spawned, so it
    # gets HOLD_EMPTY before any frame.
    assert runner.link.calls == [("hold_empty",), ("start",)]

    producer.usage_queue = [1]
    runner._pump_usage()
    filter_status(runner)
    inp = created["input"]

    frame_through(runner, inp, A, 0.0)
    runner._tick()
    assert producer.calls[-1] == ("write", BARS)

    runner._on_user_signal(signal.SIGUSR1)
    frame_through(runner, inp, C, 0.0)
    runner._tick()
    assert producer.calls[-1] == ("write", BARS)       # nothing to bridge with
    runner.link.on_output(H, 0.0, True)
    runner._tick()
    assert producer.calls[-1] == ("write", H)
    assert notes[-1] == "camera=frozen filter=none"


def test_startup_stale_flag_cleared_by_initial_usage_0(tmp_path):
    producer = FakeProducer()
    notes = []
    flag_path = str(tmp_path / "floficam-paused")
    open(flag_path, "w").close()
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0,
                    notify=notes.append, flag_path=flag_path, make_link=FakeLink)
    runner.loop = FakeLoop(producer.calls)

    runner.startup()
    producer.usage_queue = [0]
    runner._pump_usage()

    assert not os.path.exists(flag_path)
    assert runner.link.calls[-1] == ("release",)
    assert notes == ["camera=paused-card filter=down", "camera=live filter=down"]


def test_status_text_fields():
    producer = FakeProducer()
    notes = []
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0,
                    notify=notes.append, make_link=FakeLink)
    runner.startup()
    assert notes == ["camera=live filter=down"]

    runner.link.on_status({"preset": "vhs", "modified": True, "ok": True,
                           "holding": "none", "ms_per_frame": 3.0})
    assert notes == ["camera=live filter=down"]        # marshalled, not run on the link's thread
    drain_idle()
    assert notes[-1] == "camera=live filter=vhs*"

    filter_status(runner, preset="vhs")
    assert notes[-1] == "camera=live filter=vhs"
    n = len(notes)
    filter_status(runner, preset="vhs")                # unchanged: no resend
    assert len(notes) == n

    filter_status(runner, preset="vhs", ok=False)
    assert notes[-1] == "camera=live filter=error"

    runner.link.on_down()
    drain_idle()
    assert notes[-1] == "camera=live filter=down"


def test_spawn_while_paused_reasserts_hold_empty(tmp_path):
    producer = FakeProducer()
    notes = []
    flag_path = str(tmp_path / "floficam-paused")
    open(flag_path, "w").close()
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0,
                    notify=notes.append, flag_path=flag_path, make_link=FakeLink)
    runner.startup()

    runner.link.on_spawn()
    drain_idle()
    assert runner.link.calls[-1] == ("hold_empty",)
    assert notes[-1] == "camera=paused-card filter=down"


def test_respawn_while_frozen_is_paused_card():
    producer = FakeProducer()
    notes = []
    runner, inp = started_runner(producer, notify=notes.append)
    filter_status(runner)
    frame_through(runner, inp, A, 0.0)
    runner._tick()
    runner._on_user_signal(signal.SIGUSR1)
    runner.link.on_output(H, 0.0, True)
    runner._tick()
    assert notes[-1] == "camera=frozen filter=none"

    runner.link.on_spawn()
    drain_idle()
    assert runner.link.calls[-1] == ("hold_empty",)
    assert notes[-1] == "camera=paused-card filter=none"
    runner.link.on_output(H, 0.0, True)
    runner._tick()
    assert producer.calls[-1] == ("write", BARS)


def test_filter_down_while_frozen_reports_paused_card():
    producer = FakeProducer()
    notes = []
    runner, inp = started_runner(producer, notify=notes.append)
    filter_status(runner)
    frame_through(runner, inp, A, 0.0)
    runner._tick()
    runner._on_user_signal(signal.SIGUSR1)
    runner.link.on_output(H, 0.0, True)
    runner._tick()

    runner.link.on_down()
    drain_idle()
    assert notes[-1] == "camera=paused-card filter=down"
    runner._tick()
    assert producer.calls[-1] == ("write", BARS)

    runner._on_user_signal(signal.SIGUSR1)             # refused: filter_not_ready
    assert notes[-1] == "camera=paused-card filter=down"


def test_signal_resends_status_even_without_change():
    producer = FakeProducer()
    notes = []
    runner, _ = started_runner(producer, notify=notes.append)

    runner._on_user_signal(signal.SIGUSR2)
    runner._on_user_signal(signal.SIGUSR2)

    assert notes == ["camera=live filter=down", "camera=live filter=down"]


def test_refused_pause_logs_and_reports_live(capsys):
    producer = FakeProducer()
    notes = []
    runner, _ = started_runner(producer, notify=notes.append)

    runner._on_user_signal(signal.SIGUSR1)
    assert "filter_not_ready" in capsys.readouterr().err.lower()
    assert notes[-1] == "camera=live filter=down"

    filter_status(runner)
    runner._on_user_signal(signal.SIGUSR1)
    assert "no_fresh_frame" in capsys.readouterr().err.lower()
    assert notes[-1] == "camera=live filter=none"


def test_no_runtime_dir_pause_still_works():
    producer = FakeProducer()
    runner, inp = started_runner(producer, flag_path=None)
    filter_status(runner)

    frame_through(runner, inp, A, 0.0)
    runner._tick()
    runner._on_user_signal(signal.SIGUSR1)

    runner.link.on_output(H, 0.0, True)
    runner._tick()

    assert producer.calls[-1] == ("write", H)


def test_usr1_before_run_is_ignored():
    """ignore_user_signals() runs before _run() installs the GLib
    handlers; a click landing in that window must be dropped, not kill
    the process with the default disposition."""
    ignore_user_signals()
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        # Reaching here at all is the assertion: SIG_IGN swallowed it.
    finally:
        signal.signal(signal.SIGUSR1, signal.SIG_DFL)
        signal.signal(signal.SIGUSR2, signal.SIG_DFL)


def test_usr1_reaches_the_running_loop():
    """Like test_sigterm_reaches_the_running_loop: the handler is actually
    installed by _run(), on the real loop. No input is ever started, so
    the pause is refused, but the signal must still reach _on_user_signal
    and resend STATUS=live."""
    producer = FakeProducer()
    make_input, _ = make_input_factory()
    notes = []
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0, make_link=FakeLink, notify=notes.append)
    r, w = os.pipe()
    producer._fileno = r
    try:
        GLib.timeout_add(30, lambda: os.kill(os.getpid(), signal.SIGUSR1) and False)
        GLib.timeout_add(80, lambda: os.kill(os.getpid(), signal.SIGTERM) and False)
        assert runner._run() == 0
    finally:
        os.close(r)
        os.close(w)

    assert "camera=live filter=down" in notes


def test_on_user_signal_survives_a_raising_notify():
    """_on_user_signal must be as crash-proof as the tick/usage-watch
    callbacks: PyGObject drops a raising GLib source and restores SIG_DFL,
    so an uncaught exception here would silently hand the next tray click
    to the OS default (kill the process). A failed notify is not fatal."""
    producer = FakeProducer()

    def bad_notify(status):
        raise RuntimeError("socket exploded")

    runner, _ = started_runner(producer, notify=bad_notify)

    assert runner._on_user_signal(signal.SIGUSR1) == GLib.SOURCE_CONTINUE


def test_make_notify_dedups_per_status_value(capsys):
    """A failed send is logged once per distinct status value, not once
    per call — and a successful send clears every value's dedup, not just
    the one that just succeeded."""
    outcomes = [False, False, False, True, False]

    def fake_send(status):
        return outcomes.pop(0)

    notify = _make_notify(send=fake_send)

    notify("frozen")
    notify("live")
    notify("frozen")  # already logged, suppressed
    assert len(capsys.readouterr().err.strip().splitlines()) == 2

    notify("live")  # succeeds, clears the dedup for every value
    notify("frozen")  # logs again
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


# --- main(): flags and the v4l2loopback floor --------------------------

import errno  # noqa: E402

from floficam.main import main  # noqa: E402


def producer_raising(e):
    """A make_producer whose producer fails to subscribe usage events."""
    class P(FakeProducer):
        def subscribe_usage(self):
            raise OSError(e, os.strerror(e))

        def fileno(self):
            return 0

        def close(self):
            pass

    return lambda path: P()


def test_old_v4l2loopback_message(capsys):
    sent = []
    rc = main(["--device", "/dev/null"], make_producer=producer_raising(errno.ENOTTY), notify=sent.append)
    assert rc == 78
    assert "v4l2loopback >= 0.15.4 required" in capsys.readouterr().err
    assert any("v4l2loopback >= 0.15.4 required" in s for s in sent)


def test_setup_other_errno_not_floor(capsys):
    sent = []
    rc = main(["--device", "/dev/null"], make_producer=producer_raising(errno.EBUSY), notify=sent.append)
    assert rc == 1
    assert "0.15.4" not in capsys.readouterr().err
    assert any(s.startswith("could not set up /dev/null") for s in sent)


def test_open_failure_exits_1(capsys):
    def boom(path):
        raise OSError(errno.ENOENT, "nope")
    assert main(["--device", "/nonexistent"], make_producer=boom, notify=lambda s: None) == 1
    assert "could not open" in capsys.readouterr().err


def test_bad_camera_flag_exits_2():
    import pytest
    with pytest.raises(SystemExit) as ei:
        main(["--camera", "a b"], make_producer=producer_raising(errno.EBUSY), notify=lambda s: None)
    assert ei.value.code == 2


# --- device lookup, runtime file, user-facing text ---------------------

import pytest  # noqa: E402

import floficam.main as main_mod  # noqa: E402


def test_no_device_exits_with_hint(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "find_device", lambda explicit: None)
    assert main_mod.main([]) == 1
    assert ("no floficam device: create one with `floficam setup`, "
            "or enable programs.floficam.loopback on NixOS") in capsys.readouterr().err


def test_explicit_open_failure_names_setup(capsys):
    def boom(path):
        raise OSError(errno.ENOENT, "nope")
    assert main(["--device", "/nonexistent"], make_producer=boom, notify=lambda s: None) == 1
    assert "(create the device with `floficam setup`)" in capsys.readouterr().err


def test_help_has_no_internal_references(capsys):
    with pytest.raises(SystemExit):
        main_mod.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "docs/" not in out and "spec" not in out
    assert "Feed a v4l2loopback device" in out


def test_failed_start_leaves_no_runtime_file(tmp_path):
    rc = main(["--device", "/dev/null"], make_producer=producer_raising(errno.EBUSY), notify=lambda s: None)
    assert rc == 1
    assert not (tmp_path / "run" / "floficam" / "daemon.json").exists()


def test_signal_hook_called_with_refusal():
    got = []
    runner = Runner(FakeProducer(), make_input_factory()[0], bars=BARS, clock=MutableClock(),
                    make_link=FakeLink, on_signal_result=got.append)
    runner.loop = FakeLoop([])
    runner._on_user_signal(signal.SIGUSR1)
    runner._on_user_signal(signal.SIGUSR2)
    assert got == ["input_down", None]
