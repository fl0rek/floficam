"""The PipeWire camera input: the GStreamer pipeline that exists exactly
while usage is 1 (spec: "The feeder" > Running). Not unit-tested — same
as the rest of the GStreamer/V4L2 adapters, "Pure logic, tested" keeps
the pipeline itself behind a thin seam the live checks cover; main.py's
tests drive it through a fake.
"""
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

WIDTH, HEIGHT = 1280, 720

PIPELINE = (
    "pipewiresrc autoconnect=false keepalive-time=0 resend-last=false "
    # R7: without this, a PipeWire-side disconnect while usage is 1 is
    # silent (the default, "none", raises neither EOS nor ERROR), so
    # the spec's "exit non-zero on disconnect" would never fire.
    "on-disconnect=error "
    "client-name=ffsb-camera-feeder "
    'stream-properties="props,media.class=Stream/Input/Video,'
    'node.name=ffsb-camera-feed,firefox-sandbox.lane=camera-feed" ! '
    "videoconvert ! videoscale ! "
    f"video/x-raw,format=NV12,width={WIDTH},height={HEIGHT} ! "
    "appsink name=sink max-buffers=1 drop=true sync=false emit-signals=true"
)


class LatestFrame:
    """Lock-guarded holder for the newest frame. put() runs on
    GStreamer's streaming thread (the appsink callback); take() runs on
    the GLib main thread's 33 ms tick. Holds at most one frame: a tick
    that doesn't arrive in time to take one just sees it overwritten,
    which is the point (never repeat a stale real frame past the
    freshness window — logic.py enforces that window, this just avoids a
    torn read/write across threads)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None

    def put(self, frame: bytes, t: float) -> None:
        with self._lock:
            self._frame = (frame, t)

    def take(self) -> tuple[bytes, float] | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class CameraInput:
    """Builds the pipeline above on start(), tears it down on stop().
    The appsink callback never touches State — it only calls
    `latest.put()`. A bus ERROR or EOS calls `on_error` via
    `GLib.idle_add`, so it always runs on the main thread even though the
    bus watch itself fires from GStreamer's own thread."""

    def __init__(self, latest: LatestFrame, on_error):
        self.latest = latest
        self.on_error = on_error
        self.pipeline = None

    def start(self) -> None:
        if not Gst.is_initialized():
            Gst.init(None)
        self.pipeline = Gst.parse_launch(PIPELINE)
        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error_message)
        bus.connect("message::eos", self._on_eos_message)
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self.pipeline is None:
            return
        self.pipeline.get_bus().remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            self.latest.put(bytes(mapinfo.data), time.monotonic())
        finally:
            buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_error_message(self, bus, message):
        err, debug = message.parse_error()
        self._report_error(f"{err}: {debug}" if debug else str(err))

    def _on_eos_message(self, bus, message):
        self._report_error("pipewiresrc reached EOS")

    def _report_error(self, text: str) -> None:
        def deliver():
            self.on_error(text)
            return False

        GLib.idle_add(deliver)
