"""Codec crush stage (effect chain stage [7]): a JPEG or VP8 encode->decode
round trip at the crushed resolution, with VP8 "datamosh" -- withholding
delta frames from the decoder so motion smears over stale pixels. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain" row [7] and "Gates" > G2, for the semantics pinned here:
`keyframe-max-dist` is `3 * fps_cap`, a keyframe (a buffer without
`Gst.BufferFlags.DELTA_UNIT`) is never withheld, and a withheld frame
re-emits the last decoded planes (the input, before anything has
decoded).

`kind="off"` builds no pipeline at all -- `process()` is the identity by
object identity, matching every stage in stages.py at its "zero" setting.
"""
from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .settings import Codec
from .stages import Planes, from_nv12, to_nv12

# try-pull-sample's timeout, both pipelines (encoder and decoder run
# synchronously: push, then pull within this bound).
_PULL_TIMEOUT = Gst.SECOND


class CodecStage:
    def __init__(self, codec: Codec, fps_cap: int, w: int, h: int, rng) -> None:
        self._kind = codec.kind
        self._mosh = codec.mosh
        self._rng = rng
        self._w = w
        self._h = h
        self._last_decoded: Planes | None = None
        self._frame_no = 0
        self._duration = Gst.SECOND // fps_cap

        if self._kind == "off":
            self._enc_pipeline = None
            self._dec_pipeline = None
            return

        if not Gst.is_initialized():
            Gst.init(None)

        src_caps = f"video/x-raw,format=NV12,width={w},height={h},framerate={fps_cap}/1"
        if self._kind == "jpeg":
            encoder = f"jpegenc quality={codec.jpeg_quality}"
        elif self._kind == "vp8":
            encoder = (
                "vp8enc deadline=1 "
                f"keyframe-max-dist={3 * fps_cap} "
                f"target-bitrate={codec.vp8_kbps * 1000} lag-in-frames=0"
            )
        else:
            raise ValueError(f"CodecStage: unknown codec kind {self._kind!r}")

        self._enc_pipeline = Gst.parse_launch(
            f"appsrc name=src format=time caps={src_caps} ! "
            "videoconvert ! video/x-raw,format=I420 ! "
            f"{encoder} ! "
            "appsink name=sink emit-signals=false sync=false"
        )
        self._enc_src = self._enc_pipeline.get_by_name("src")
        self._enc_sink = self._enc_pipeline.get_by_name("sink")

        decoder = "vp8dec" if self._kind == "vp8" else "jpegdec"
        self._dec_pipeline = Gst.parse_launch(
            f"appsrc name=src format=time ! {decoder} ! videoconvert ! "
            f"video/x-raw,format=NV12,width={w},height={h} ! "
            "appsink name=sink emit-signals=false sync=false"
        )
        self._dec_src = self._dec_pipeline.get_by_name("src")
        self._dec_sink = self._dec_pipeline.get_by_name("sink")

        # No get_state() wait here: an appsrc/appsink pipeline can't
        # preroll (reach PLAYING) until its first buffer arrives, so
        # blocking on that now would deadlock before process() ever
        # gets to push one. set_state(PLAYING) queues the transition;
        # it completes once push-buffer/push-sample start feeding it.
        self._enc_pipeline.set_state(Gst.State.PLAYING)
        self._dec_pipeline.set_state(Gst.State.PLAYING)

    def process(self, p: Planes) -> Planes:
        if self._kind == "off":
            return p

        buf = Gst.Buffer.new_wrapped(to_nv12(p))
        buf.pts = self._frame_no * self._duration
        buf.duration = self._duration
        self._frame_no += 1

        self._enc_src.emit("push-buffer", buf)
        sample = self._enc_sink.emit("try-pull-sample", _PULL_TIMEOUT)
        if sample is None:
            return self._fallback(p)

        enc_buf = sample.get_buffer()
        is_keyframe = not bool(enc_buf.get_flags() & Gst.BufferFlags.DELTA_UNIT)
        if not is_keyframe and self._rng.random() < self._mosh:
            return self._fallback(p)

        self._dec_src.emit("push-sample", sample)
        dec_sample = self._dec_sink.emit("try-pull-sample", _PULL_TIMEOUT)
        if dec_sample is None:
            return self._fallback(p)

        dec_buf = dec_sample.get_buffer()
        ok, mapinfo = dec_buf.map(Gst.MapFlags.READ)
        if not ok:
            return self._fallback(p)
        try:
            planes = from_nv12(bytes(mapinfo.data), self._w, self._h)
        finally:
            dec_buf.unmap(mapinfo)

        self._last_decoded = planes
        return planes

    def _fallback(self, p: Planes) -> Planes:
        return self._last_decoded if self._last_decoded is not None else p

    def close(self) -> None:
        if self._kind == "off":
            return
        self._enc_pipeline.set_state(Gst.State.NULL)
        self._dec_pipeline.set_state(Gst.State.NULL)
