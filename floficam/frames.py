"""SMPTE bars, rendered once at start-up (the spec's start-up step 3) and
kept as both the timeout image and the idle-flush frame."""
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def frame_size(w: int, h: int) -> int:
    """NV12: one luma byte per pixel plus one subsampled 4:2:0 chroma
    pair per 2x2 block."""
    return w * h * 3 // 2


def render_bars(w: int, h: int) -> bytes:
    """One SMPTE colour-bars frame at `w`x`h` NV12, via `videotestsrc
    pattern=smpte num-buffers=1`."""
    if not Gst.is_initialized():
        Gst.init(None)

    pipeline = Gst.parse_launch(
        "videotestsrc pattern=smpte num-buffers=1 ! "
        f"video/x-raw,format=NV12,width={w},height={h},framerate=30/1 ! "
        "appsink name=sink sync=false max-buffers=1"
    )
    sink = pipeline.get_by_name("sink")
    try:
        pipeline.set_state(Gst.State.PLAYING)
        sample = sink.emit("pull-preroll")
        if sample is None:
            raise RuntimeError("videotestsrc produced no sample")
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("could not map the rendered bars buffer")
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        return data
    finally:
        pipeline.set_state(Gst.State.NULL)
