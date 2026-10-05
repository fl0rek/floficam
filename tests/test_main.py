"""The feeder's GLib-driven runtime: start-up order, action effects, and
the two error paths that must exit non-zero so systemd restarts the
service. Fakes only: no real V4L2 device, no real PipeWire. See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Start-up and Running.
"""
import threading
import time

from ffsb_camera import v4l2
from ffsb_camera.logic import FLUSH_FRAMES, Act, on_tick, on_usage
from ffsb_camera.main import Runner

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
        return -1

    def close(self):
        self.calls.append(("close",))


class FakeInput:
    """Stands in for pwinput.CameraInput. Keeps the `on_error` it was
    built with so a test can fire it, the way a real bus ERROR would
    (marshalled onto the main thread first, which is pwinput's job, not
    Runner's)."""

    def __init__(self, latest, on_error):
        self.latest = latest
        self.on_error = on_error
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_input_factory():
    """A make_input callable plus a way to reach the FakeInput it built."""
    created = {}

    def make_input(latest, on_error):
        inp = FakeInput(latest, on_error)
        created["input"] = inp
        return inp

    return make_input, created


def test_startup_order():
    producer = FakeProducer()
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

    runner.startup()

    assert producer.calls == [
        ("configure", 1280, 720, "NV12"),
        ("set_timeout", 1000),
        ("install_timeout_image", BARS),
        ("write", BARS),
        ("subscribe_usage",),
    ]


def test_flush_writes_two_bars_then_stops_input():
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

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
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

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
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

    assert runner.main() == 1


def test_timeout_image_lost_exits_1():
    error = v4l2.TimeoutImageLost("REQBUFS failed: fake")
    producer = FakeProducer(install_timeout_image_error=error)
    make_input, _ = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

    assert runner.main() == 1


def test_frame_from_thread_does_not_race():
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

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
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

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
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

    result = runner._on_usage_ready(producer.fileno(), 0)

    assert result is False
    assert runner.exit_code == 1


def test_stop_input_drains_latest_frame():
    """A frame that lands just before STOP_INPUT must not survive it: if
    usage returns within the freshness window, the next WRITE_REAL must
    not resurrect a frame from before the lane was torn down."""
    producer = FakeProducer()
    make_input, created = make_input_factory()
    runner = Runner(producer, make_input, bars=BARS, clock=lambda: 0.0)

    producer.usage_queue = [1]
    runner._pump_usage()
    runner.latest.put(b"stale real frame", 0.0)
    runner._last_frame = b"stale real frame"

    runner.handle([Act.STOP_INPUT])

    assert created["input"].stopped is True
    assert runner.latest.take() is None
    assert runner._last_frame is None
