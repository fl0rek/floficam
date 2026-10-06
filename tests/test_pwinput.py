"""CameraInput's threading (R9): state changes run on a worker thread, so a
`pipewiresrc` start that waits for a link never blocks the GLib main
thread, and stop() cancels such a start. Fake pipelines only: no
PipeWire. See pwinput.py's module docstring.
"""
import threading
import time

from ffsb_camera.logic import Act
from ffsb_camera.main import Runner
from ffsb_camera.pwinput import CameraInput, _Worker
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

    def __init__(self, name, log):
        self.name = name
        self.log = log
        self.released = threading.Event()
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
        make_input=lambda latest, on_error: CameraInput(
            latest, on_error, make_pipeline=lambda: pipelines[0], worker=worker),
        bars=b"B",
        clock=lambda: 0.0,
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

    def make(latest, on_error):
        return CameraInput(latest, on_error, make_pipeline=lambda: queue.pop(0), worker=worker)

    first = make(None, None)
    first.start()
    assert a.playing_entered.wait(2)
    first.stop()
    second = make(None, None)
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
        return CameraInput(None, None, make_pipeline=lambda: queue.pop(0), worker=worker)

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
    inp = CameraInput(None, errors.append, make_pipeline=lambda: FakePipeline("a", log), worker=worker)
    inp.start()
    inp._report_error("before stop")
    inp.stop()
    inp._report_error("after stop")

    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass

    # Both idles ran after stop(), so neither reaches Runner.
    assert errors == []
