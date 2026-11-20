"""CameraInput's threading (R9): state changes run on a worker thread, so a
`pipewiresrc` start that waits for a link never blocks the GLib main
thread, and stop() cancels such a start. Fake pipelines only: no
PipeWire. See pwinput.py's module docstring.
"""
import threading
import time

import pytest

from floficam.logic import Act
from floficam.main import Runner
from floficam.pwinput import REBUILD_MAX, CameraInput, _Job, _Worker
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
            sink, epoch, on_error, "unused", make_pipeline=lambda: pipelines[0], worker=worker),
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
        return CameraInput(sink, epoch, on_error, pipeline="unused", make_pipeline=lambda: queue.pop(0), worker=worker)

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
        return CameraInput(None, 0, None, pipeline="unused", make_pipeline=lambda: queue.pop(0), worker=worker)

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
    inp = CameraInput(None, 0, errors.append, pipeline="unused", make_pipeline=lambda: FakePipeline("a", log), worker=worker)
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
                      pipeline="unused", make_pipeline=lambda: pipeline, worker=worker)
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
            t.name == "floficam-gst-cancel" and t.is_alive() for t in threading.enumerate()), 3)
    finally:
        pipeline.released.set()
    assert "floficam-gst-cancel" not in before


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

    inp = CameraInput(None, 0, errors.append, pipeline="unused", make_pipeline=make, worker=_Worker(),
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
                      pipeline="unused", make_pipeline=lambda: None, worker=_Worker())
    t0 = time.monotonic()
    assert inp._on_new_sample(_Appsink(b"raw")) == Gst.FlowReturn.OK
    (frame, t, epoch), = got
    assert (frame, epoch) == (b"raw", 7)
    assert t0 <= t <= time.monotonic()

    inp._stopped = True
    assert inp._on_new_sample(_Appsink(b"late")) == Gst.FlowReturn.OK
    assert len(got) == 1


# --- build_pipeline / parse_stream_property ----------------------------

OLD_PIPELINE = (
    "pipewiresrc name=src autoconnect=false keepalive-time=0 resend-last=false "
    "client-name=ffsb-camera-feeder "
    'stream-properties="props,media.class=Stream/Input/Video,'
    'node.name=ffsb-camera-feed,firefox-sandbox.lane=camera-feed" ! '
    "videoconvert ! videoscale ! "
    "video/x-raw,format=NV12,width=1280,height=720 ! "
    "appsink name=sink max-buffers=1 drop=true sync=false emit-signals=true"
)


def test_external_pipeline_is_todays():
    from floficam.pwinput import build_pipeline
    today = OLD_PIPELINE.replace("client-name=ffsb-camera-feeder", "client-name=floficam") \
                        .replace("node.name=ffsb-camera-feed", "node.name=floficam-feed")
    assert build_pipeline("external", {"firefox-sandbox.lane": "camera-feed"}) == today


def test_target_pipeline():
    from floficam.pwinput import build_pipeline
    p = build_pipeline("v4l2_input.platform-isp", {})
    assert "autoconnect=true" in p and "target-object=v4l2_input.platform-isp" in p


def test_stream_property_rejects_unsafe():
    from floficam.pwinput import parse_stream_property
    for bad in ["a=b,c", 'a="x"', "a=b c", "a=b\n", "a=%i", "a=b\\x", "=x", "noequals",
                "node.name=x", "media.class=x"]:
        with pytest.raises(ValueError):
            parse_stream_property(bad)


def test_stream_property_parses():
    from floficam.pwinput import parse_stream_property
    assert parse_stream_property("a.b=c:d") == ("a.b", "c:d")


def test_target_rejects_unsafe():
    from floficam.pwinput import build_pipeline
    for bad in ["a b", 'a"', "a,b", "", "%h"]:
        with pytest.raises(ValueError):
            build_pipeline(bad, {})


# --- --camera auto: the lookup runs in the start job -------------------

from floficam.cameras import Camera  # noqa: E402

CAM = Camera("v4l2_input.platform-isp", "FaceTime HD", "/dev/video0")


class RecordingWorker:
    """Records submitted jobs; the test runs them by hand."""

    deadline = 10.0

    def __init__(self):
        self.jobs = []

    def submit(self, fn, on_overdue=None, waits=False):
        self.jobs.append((fn, on_overdue, waits))
        return _Job(fn, on_overdue, waits)


class AutoPipeline:
    """Records pipewiresrc property sets and state changes, in order."""

    def __init__(self, log):
        self.log = log
        pipeline = self

        class Src:
            def get_property(self, name):
                assert name == "stream-properties"
                return Gst.Structure.new_from_string("props,media.class=Stream/Input/Video")

            def set_property(self, name, value):
                if name == "stream-properties":
                    value = {value.nth_field_name(i): value.get_value(value.nth_field_name(i))
                             for i in range(value.n_fields())}
                pipeline.log.append(("set", name, value))

            def get_static_pad(self, name):
                class Pad:
                    def send_event(self, event):
                        return True
                return Pad()

        self._src = Src()
        self._stub = _Stub()

    def get_by_name(self, name):
        return self._src if name == "src" else self._stub

    def get_bus(self):
        return self._stub

    def set_state(self, state):
        self.log.append(("state", state))
        return Gst.StateChangeReturn.ASYNC


@pytest.fixture
def idle_now(monkeypatch):
    """GLib.idle_add runs the callback at once, as if on the main thread."""
    monkeypatch.setattr(GLib, "idle_add", lambda fn, *args: fn(*args))


def auto_input(log, pick, worker, got=None, on_error=None):
    return CameraInput(None, 0, on_error, pipeline="unused", make_pipeline=lambda: AutoPipeline(log),
                       worker=worker, pick_camera=pick,
                       on_camera=(got.append if got is not None else None))


def test_auto_looks_up_in_the_worker_then_sets_target_before_playing(idle_now):
    log, got, calls = [], [], []
    worker = RecordingWorker()

    def pick():
        calls.append(threading.current_thread())
        return CAM

    auto_input(log, pick, worker, got).start()
    assert calls == [] and log == []           # start() did no lookup
    assert len(worker.jobs) == 2
    (lookup, lookup_overdue, lookup_waits), (play, _, play_waits) = worker.jobs
    assert lookup_waits is False and lookup_overdue is not None   # under the deadline
    assert play_waits is True

    lookup()
    assert len(calls) == 1
    assert got == [CAM]
    play()
    assert log == [("set", "stream-properties", {"media.class": "Stream/Input/Video",
                                                 "node.dont-fallback": "true",
                                                 "node.dont-reconnect": "true"}),
                   ("set", "target-object", "v4l2_input.platform-isp"),
                   ("set", "autoconnect", True),
                   ("state", Gst.State.PLAYING)]


def test_auto_none_leaves_the_input_unlinked(idle_now):
    log, got = [], []
    worker = RecordingWorker()
    auto_input(log, lambda: None, worker, got).start()
    for fn, _, _ in worker.jobs:
        fn()
    assert got == [None]
    # Never started: no PipeWire node, nothing to link, bars. A started
    # never-linked pipewiresrc could not be stopped (see CameraInput).
    assert log == []


def test_auto_lookup_failure_counts_as_none(idle_now, capsys):
    log, got = [], []
    worker = RecordingWorker()

    def boom():
        raise RuntimeError("no pipewire")

    auto_input(log, boom, worker, got).start()
    for fn, _, _ in worker.jobs:
        fn()
    assert got == [None]
    assert log == []
    assert "no pipewire" in capsys.readouterr().err


def test_auto_stop_before_lookup_skips_it(idle_now):
    log, calls = [], []
    worker = RecordingWorker()
    inp = auto_input(log, lambda: calls.append(1), worker)
    inp.start()
    lookup, play = worker.jobs[0][0], worker.jobs[1][0]
    inp.stop()
    lookup()
    play()
    assert calls == []
    assert ("state", Gst.State.PLAYING) not in log


def test_auto_rebuild_reuses_the_lookup(idle_now):
    log, calls, got = [], [], []
    worker = RecordingWorker()

    def pick():
        calls.append(1)
        return CAM

    inp = auto_input(log, pick, worker, got)
    inp.start()
    for fn, _, _ in list(worker.jobs):
        fn()
    worker.jobs.clear()
    a = inp._attempt
    inp._rebuild(a, "not negotiated")
    for fn, _, _ in list(worker.jobs):
        fn()
    assert calls == [1] and got == [CAM]
    assert log.count(("set", "target-object", "v4l2_input.platform-isp")) == 2


def test_wedged_lookup_is_reported_overdue():
    errors = []
    release = threading.Event()
    worker = _Worker(deadline=0.2, tick=0.05)
    inp = CameraInput(None, 0, lambda text, overdue=False: errors.append((text, overdue)),
                      pipeline="unused", make_pipeline=lambda: AutoPipeline([]), worker=worker,
                      pick_camera=lambda: release.wait(5) and None, on_camera=lambda cam: None)
    inp.start()
    try:
        ctx = GLib.MainContext.default()
        assert wait_for(lambda: (ctx.iteration(False), bool(errors))[1], 3)
        text, overdue = errors[0]
        assert overdue is True and "camera lookup" in text
    finally:
        release.set()


def test_real_pipewiresrc_takes_the_target_at_null():
    """The properties the lookup sets exist on the real element and are
    settable at NULL (pipewiresrc reads them when it opens and connects
    the stream). Parsed at NULL: no PipeWire connection is made. The
    stream also gets node.dont-fallback/node.dont-reconnect, so
    WirePlumber never links or moves it to a camera usable() rejected:
    target gone, the stream stays unlinked (bars) or errors out."""
    from floficam.pwinput import build_pipeline

    if not Gst.is_initialized():
        Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline("auto", {"firefox-sandbox.lane": "camera-feed"}))
    src = pipeline.get_by_name("src")
    assert src.props.autoconnect is False and src.props.target_object is None
    assert not src.props.stream_properties.has_field("node.dont-fallback")
    worker = RecordingWorker()
    inp = CameraInput(None, 0, None, pipeline="unused", make_pipeline=lambda: pipeline, worker=worker,
                      pick_camera=lambda: CAM, on_camera=lambda cam: None)
    inp.start()
    worker.jobs[0][0]()  # the lookup only; never PLAYING
    assert src.props.target_object == "v4l2_input.platform-isp"
    assert src.props.autoconnect is True
    props = src.props.stream_properties
    assert props.get_value("node.dont-fallback") == "true"
    assert props.get_value("node.dont-reconnect") == "true"
    # The rest of the stream properties survive.
    assert props.get_value("node.name") == "floficam-feed"
    assert props.get_value("media.class") == "Stream/Input/Video"
    assert props.get_value("firefox-sandbox.lane") == "camera-feed"
    pipeline.get_bus().remove_signal_watch()


def test_none_found_leaves_stream_properties_alone():
    from floficam.pwinput import build_pipeline

    if not Gst.is_initialized():
        Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline("auto", {}))
    src = pipeline.get_by_name("src")
    before = src.props.stream_properties.to_string()
    worker = RecordingWorker()
    CameraInput(None, 0, None, pipeline="unused", make_pipeline=lambda: pipeline, worker=worker,
                pick_camera=lambda: None, on_camera=lambda cam: None).start()
    worker.jobs[0][0]()
    assert src.props.stream_properties.to_string() == before
    assert src.props.autoconnect is False
    pipeline.get_bus().remove_signal_watch()


def test_auto_pipeline_starts_like_external():
    from floficam.pwinput import build_pipeline
    assert build_pipeline("auto", {"a": "b"}) == build_pipeline("external", {"a": "b"})


def test_is_safe_target():
    from floficam.pwinput import is_safe_target
    assert is_safe_target("v4l2_input.platform-22a000000.isp")
    assert is_safe_target("113")
    for bad in ["", "a b", 'a"', "a,b", "%h", "a\n"]:
        assert not is_safe_target(bad)


def test_auto_none_stops_at_once():
    """No camera found: the stop that follows has nothing to wait for, so
    it never runs into the deadline (on a real pipewiresrc that was
    started and never linked, set_state(NULL) blocks for good)."""
    log, errors = [], []
    worker = _Worker(deadline=0.3, tick=0.05)
    inp = CameraInput(None, 0, lambda text, overdue=False: errors.append(text),
                      pipeline="unused", make_pipeline=lambda: FakePipeline("a", log), worker=worker,
                      pick_camera=lambda: None, on_camera=lambda cam: None)
    inp.start()
    assert wait_for(lambda: inp._attempt is not None and inp._attempt.start_done.is_set())
    inp.stop()
    assert wait_for(lambda: ("a", "null") in log)
    assert log == [("a", "null")]  # never PLAYING, so no wait for a link
    time.sleep(0.5)
    drain_main_context()
    assert errors == []
