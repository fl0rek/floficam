"""CameraInput's threading (R9): state changes run on a worker thread, so a
`pipewiresrc` start that waits for a link never blocks the GLib main
thread, and stop() cancels such a start. Fake pipelines only: no
PipeWire. See pwinput.py's module docstring.
"""
import threading
import time

from ffsb_camera.logic import Act
from ffsb_camera.main import Runner
from ffsb_camera.pwinput import REBUILD_MAX, CameraInput, _Worker
from gi.repository import GLib, Gst  # after pwinput's gi.require_version


class _Stub:
    def connect(self, *args):
        pass

    def add_signal_watch(self):
        pass

    def remove_signal_watch(self):
        pass


class FakePipeline:
    """set_state(PLAYING) blocks, like pipewiresrc with no link, until a
    flush-start reaches the source pad (or `link()` is called)."""

    def __init__(self, name, log, blocks=True):
        self.name = name
        self.log = log
        self.released = threading.Event()
        if not blocks:
            self.released.set()
        self.playing_entered = threading.Event()
        pipeline = self

        class Pad:
            def send_event(self, event):
                if event.type == Gst.EventType.FLUSH_START:
                    pipeline.released.set()
                return True

        class Src:
            def get_static_pad(self, name):
                return Pad()

        self._src = Src()
        self._stub = _Stub()

    def get_by_name(self, name):
        return self._src if name == "src" else self._stub

    def get_bus(self):
        return self._stub

    def link(self):
        self.released.set()

    def set_state(self, state):
        if state == Gst.State.PLAYING:
            self.log.append((self.name, "playing-enter"))
            self.playing_entered.set()
            assert self.released.wait(5), "PLAYING never released"
            self.log.append((self.name, "playing-return"))
            return Gst.StateChangeReturn.FAILURE
        self.log.append((self.name, "null"))
        return Gst.StateChangeReturn.SUCCESS


class _StubLink:
    """Runner's `make_link` here: the input's threading is under test, not
    the filter child, so no real FilterLink is ever built."""

    def __init__(self, *callbacks):
        pass

    last_input_t = None

    def reset(self, epoch):
        pass

    def submit(self, frame, input_t, epoch):
        pass


class FakeProducer:
    def write(self, frame):
        return len(frame)

    def set_timeout(self, ms):
        pass


def wait_for(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_blocking_start_does_not_block_handle():
    log = []
    pipelines = [FakePipeline("a", log)]
    worker = _Worker()
    runner = Runner(
        FakeProducer(),
        make_input=lambda sink, epoch, on_error: CameraInput(
            sink, epoch, on_error, make_pipeline=lambda: pipelines[0], worker=worker),
        bars=b"B",
        clock=lambda: 0.0,
        make_link=_StubLink,
    )

    t0 = time.monotonic()
    runner.handle([Act.START_INPUT])
    assert time.monotonic() - t0 < 0.5
    assert pipelines[0].playing_entered.wait(2)  # the worker is now stuck in it

    t0 = time.monotonic()
    runner.handle([Act.STOP_INPUT])
    assert time.monotonic() - t0 < 0.5

    # stop() cancelled the start (flush-start), then set NULL after it.
    assert wait_for(lambda: ("a", "null") in log)
    assert log == [("a", "playing-enter"), ("a", "playing-return"), ("a", "null")]


def test_next_start_waits_for_previous_stop():
    """FIFO: input b's start never overtakes input a's NULL, so two
    feeder inputs never coexist."""
    log = []
    a, b = FakePipeline("a", log), FakePipeline("b", log)
    queue = [a, b]
    worker = _Worker()

    def make(sink, epoch, on_error):
        return CameraInput(sink, epoch, on_error, make_pipeline=lambda: queue.pop(0), worker=worker)

    first = make(None, 0, None)
    first.start()
    assert a.playing_entered.wait(2)
    first.stop()
    second = make(None, 0, None)
    second.start()
    assert b.playing_entered.wait(3)
    b.link()

    assert wait_for(lambda: ("b", "playing-return") in log)
    assert log.index(("a", "null")) < log.index(("b", "playing-enter"))


def test_stop_before_start_runs_skips_playing():
    """A start still queued behind another job is dropped by stop()."""
    log = []
    blocker, a = FakePipeline("blocker", log), FakePipeline("a", log)
    queue = [blocker, a]
    worker = _Worker()

    def make():
        return CameraInput(None, 0, None, make_pipeline=lambda: queue.pop(0), worker=worker)

    held = make()
    held.start()
    assert blocker.playing_entered.wait(2)
    inp = make()
    inp.start()
    inp.stop()  # a's start is still queued behind the blocker
    blocker.link()

    assert wait_for(lambda: ("a", "null") in log)
    assert ("a", "playing-enter") not in log


def test_no_error_delivered_after_stop():
    log, errors = [], []
    worker = _Worker()
    inp = CameraInput(None, 0, errors.append, make_pipeline=lambda: FakePipeline("a", log), worker=worker)
    inp.start()
    attempt = inp._attempt
    inp._report_error("before stop", attempt)
    inp.stop()
    inp._report_error("after stop", attempt)

    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass

    # Both idles ran after stop(), so neither reaches Runner.
    assert errors == []


def test_worker_survives_a_raising_job():
    worker = _Worker()
    ran = threading.Event()

    def boom():
        raise RuntimeError("set_state exploded")

    worker.submit(boom)
    worker.submit(ran.set)
    assert ran.wait(2)


def test_job_past_deadline_is_reported():
    worker = _Worker(deadline=0.2, tick=0.05)
    release = threading.Event()
    overdue = threading.Event()
    worker.submit(lambda: release.wait(5), on_overdue=overdue.set)
    try:
        assert overdue.wait(2)
    finally:
        release.set()


def test_waiting_start_reported_only_after_cancel():
    """A start waiting for its link is legitimate (lane locked) until
    stop() cancels it; then the deadline runs."""
    worker = _Worker(deadline=0.2, tick=0.05)
    release = threading.Event()
    overdue = threading.Event()
    job = worker.submit(lambda: release.wait(5), on_overdue=overdue.set, waits=True)
    try:
        assert not overdue.wait(0.6)
        job.cancel()
        assert overdue.wait(2)
    finally:
        release.set()


class StuckPipeline(FakePipeline):
    """Ignores flush-start: PLAYING never returns until released."""

    def get_by_name(self, name):
        stub = super().get_by_name(name)
        if name != "src":
            return stub

        class Pad:
            def send_event(self, event):
                return True

        class Src:
            def get_static_pad(self, name):
                return Pad()

        return Src()


def test_stuck_start_after_stop_reaches_on_error_and_cancel_ends():
    log, errors = [], []
    pipeline = StuckPipeline("a", log)
    worker = _Worker(deadline=0.3, tick=0.05)
    inp = CameraInput(None, 0, lambda text, overdue=False: errors.append((text, overdue)),
                      make_pipeline=lambda: pipeline, worker=worker)
    inp.start()
    assert pipeline.playing_entered.wait(2)
    before = {t.name for t in threading.enumerate()}
    inp.stop()
    try:
        ctx = GLib.MainContext.default()
        assert wait_for(lambda: (ctx.iteration(False), bool(errors))[1], 3)
        text, overdue = errors[0]
        assert "start" in text
        assert overdue is True  # Runner exits on it at any usage level
        # The flush-start retry thread gives up after the deadline.
        assert wait_for(lambda: not any(
            t.name == "ffsb-gst-cancel" and t.is_alive() for t in threading.enumerate()), 3)
    finally:
        pipeline.released.set()
    assert "ffsb-gst-cancel" not in before


class FakeErrorMessage:
    """A bus ERROR as basesrc posts a flow error: STREAM/FAILED with
    details flow-return."""

    def __init__(self, src, flow=Gst.FlowReturn.NOT_NEGOTIATED, code=Gst.StreamError.FAILED,
                 domain=Gst.StreamError):
        self.src = src
        self._err = GLib.Error.new_literal(domain.quark(), "Internal data stream error.", int(code))
        self._details = Gst.Structure.new_from_string(f"details, flow-return=(int){int(flow)}")

    def parse_error(self):
        return self._err, "streaming stopped, reason not-negotiated (-4)"

    def parse_error_details(self):
        return self._details


def drain_main_context():
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass


def rebuild_rig(now):
    log, errors, made = [], [], []

    def make():
        p = FakePipeline(f"p{len(made)}", log, blocks=False)
        made.append(p)
        return p

    inp = CameraInput(None, 0, errors.append, make_pipeline=make, worker=_Worker(),
                      clock=lambda: now[0])
    inp.start()
    return inp, errors, made, log


def race(inp):
    """The lock-raced error, from the current pipeline's own pipewiresrc."""
    a = inp._attempt
    inp._on_error_message(None, FakeErrorMessage(a.pipeline.get_by_name("src")), a)
    inp._on_eos_message(None, None, a)  # basesrc's EOS that follows
    drain_main_context()


def test_not_negotiated_rebuilds_without_on_error():
    now = [0.0]
    inp, errors, made, log = rebuild_rig(now)

    race(inp)

    assert errors == []
    assert len(made) == 2 and inp._attempt.pipeline is made[1]
    assert wait_for(lambda: ("p0", "null") in log and ("p1", "playing-enter") in log)
    assert log.index(("p0", "null")) < log.index(("p1", "playing-enter"))


def test_fourth_not_negotiated_within_window_reports():
    now = [0.0]
    inp, errors, made, _ = rebuild_rig(now)

    for i in range(REBUILD_MAX):
        now[0] = i * 2.0
        race(inp)
    assert errors == [] and len(made) == REBUILD_MAX + 1

    now[0] = 9.0
    race(inp)
    # Reported like any error (basesrc's trailing EOS follows it).
    assert errors and "not-negotiated 4 times" in errors[0]
    assert len(made) == REBUILD_MAX + 1  # no further rebuild


def test_not_negotiated_outside_window_rebuilds_again():
    now = [0.0]
    inp, errors, made, _ = rebuild_rig(now)
    for t in (0.0, 1.0, 2.0, 12.5):  # the first two have left the window
        now[0] = t
        race(inp)
    assert errors == [] and len(made) == 5


def test_other_errors_still_report():
    now = [0.0]
    inp, errors, made, _ = rebuild_rig(now)
    a = inp._attempt
    src = a.pipeline.get_by_name("src")

    inp._on_error_message(None, FakeErrorMessage(src, flow=Gst.FlowReturn.ERROR), a)
    drain_main_context()
    assert len(errors) == 1 and len(made) == 1

    inp._on_error_message(None, FakeErrorMessage(object()), a)  # not our pipewiresrc
    inp._on_eos_message(None, None, a)  # a bare EOS
    drain_main_context()
    assert len(errors) == 3 and len(made) == 1


def test_not_negotiated_after_stop_does_nothing():
    now = [0.0]
    inp, errors, made, _ = rebuild_rig(now)
    a = inp._attempt
    inp.stop()
    inp._on_error_message(None, FakeErrorMessage(a.pipeline.get_by_name("src")), a)
    drain_main_context()
    assert errors == [] and len(made) == 1


class _Sample:
    """Just enough of a GstSample for _on_new_sample."""

    def __init__(self, data):
        self.data = data

    def get_buffer(self):
        sample = self

        class Buf:
            def map(self, flags):
                class Info:
                    data = sample.data
                return True, Info()

            def unmap(self, info):
                pass

        return Buf()


class _Appsink:
    def __init__(self, data):
        self.data = data

    def emit(self, name):
        assert name == "pull-sample"
        return _Sample(self.data)


def test_new_sample_goes_to_sink_with_arrival_time_and_epoch():
    """The appsink callback never touches LatestFrame: it hands the raw
    frame, its arrival time and the input's epoch to `sink`
    (FilterLink.submit in production)."""
    got = []
    inp = CameraInput(lambda frame, t, epoch: got.append((frame, t, epoch)), 7, None,
                      make_pipeline=lambda: None, worker=_Worker())
    t0 = time.monotonic()
    assert inp._on_new_sample(_Appsink(b"raw")) == Gst.FlowReturn.OK
    (frame, t, epoch), = got
    assert (frame, epoch) == (b"raw", 7)
    assert t0 <= t <= time.monotonic()

    inp._stopped = True
    assert inp._on_new_sample(_Appsink(b"late")) == Gst.FlowReturn.OK
    assert len(got) == 1
