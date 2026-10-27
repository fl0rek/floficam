"""The effect chain: one call per input frame. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain" and "The held flag", for the semantics pinned here:

    in 1280x720 -> (1) temporal gate -> (2) downscale -> (3) shutter ->
    (4) tint -> (5) colour -> (6) chroma bleed -> (7) codec ->
    (8) upscale -> (9) interlace -> (10) scanlines -> (11) grain -> out

Holding: `HOLD_GRAB` arms the chain to grab its *next* input as the held
frame (so a `HOLD_GRAB` that races a live frame never captures live
pixels after the fact); every call after that runs the chain on the held
frame and flags the output. Between `HOLD_GRAB` and that next call,
nothing is held yet, so `holding` reports `"empty"`, same as `HOLD_EMPTY`
-- it flips to `"frame"` only once the grab actually captures a frame.
`HOLD_EMPTY` emits nothing at all, and never captures. `RELEASE` and
`reset()` both drop back to live; `reset()` additionally clears the
temporal state (ghost and interlace buffers, the codec pipeline, the
gate's last output and stall schedule). `set_knobs` never touches hold
state -- a settings reload must not un-pause a frozen call.

The temporal gate re-emits the last *output* without running stages
2-11 when the fps cap says the input came too soon, or while a stall
lasts. Both run on pts time, never the wall clock. A cap of 30 is the
gate's zero setting (no cap), like every other stage's. The call that
captures a held frame bypasses both, so the first held output is never a
re-emit of live-derived pixels.

With every pixel knob at its zero setting the frame's bytes pass through
untouched (no NV12 round trip), which is what keeps `none` an exact
identity at no cost.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import stages
from .codec import CodecStage
from .settings import Knobs

_NONE = "none"
_EMPTY = "empty"
_FRAME = "frame"

W, H = 1280, 720
_NO_CAP = 30
_CAP_SLACK_NS = 5_000_000


class ChainConfigError(Exception):
    """The knobs can't be realised (the codec pipeline failed to build,
    e.g. a missing GStreamer plugin). The child reports it like a
    settings error: `ok: false`, the message, and no frames."""


@dataclass(frozen=True)
class Output:
    frame: bytes
    held: bool


def _pixels_identity(k: Knobs) -> bool:
    pic, a = k.picture, k.analog
    return (
        pic.width == W
        and pic.shutter == 0.0
        and pic.tint == "none"
        and pic.colour == "true"
        and k.codec.kind == "off"
        and not a.interlace
        and a.chroma_shift == 0
        and a.chroma_smear == 0
        and a.scanlines == 0.0
        and a.grain == 0.0
    )


def _codec_key(k: Knobs) -> tuple:
    return (k.codec, k.temporal.fps_cap, k.picture.width)


class Chain:
    def __init__(self, knobs: Knobs, rng) -> None:
        """`rng` is a numpy Generator: stalls, mosh and grain all draw
        from it. Raises ChainConfigError if the codec stage can't be
        built."""
        self._rng = rng
        self._holding = _NONE
        self._armed = False  # HOLD_GRAB issued, waiting for the next input
        self._held_frame: bytes | None = None
        self._codec: CodecStage | None = None
        self._knobs = knobs
        self._reset_gate()
        self._new_stages()
        self._build_codec()

    @property
    def holding(self) -> str:
        return self._holding

    def process(self, frame: bytes, pts_ns: int) -> Output | None:
        grab = self._armed
        if grab:
            # The grab: capture this input as the held frame. Checked
            # before the `_EMPTY` early-return below, since `hold_grab`
            # reports "empty" (nothing held yet) right up to this call.
            self._held_frame = frame
            self._armed = False
            self._holding = _FRAME
        elif self._holding == _EMPTY:
            return None

        if self._codec is None:
            return None  # a failed codec build: fail closed until set_knobs

        held = self._holding == _FRAME
        if held:
            frame = self._held_frame

        if not grab and self._gated(pts_ns):
            return Output(self._last_out, held)

        self._last_work_pts = pts_ns
        self._last_out = self._run_pixels(frame)
        return Output(self._last_out, held)

    # --- (1) temporal gate ---------------------------------------------------

    def _gated(self, pts_ns: int) -> bool:
        """True if this input re-emits the last output. Also advances the
        stall schedule, so it runs on every non-grab call."""
        if self._last_out is None or pts_ns < self._last_work_pts:
            # The first frame always runs; pts going backwards means a
            # new input, so the old schedule means nothing for it.
            self._reset_gate()
            return False

        stalled = self._in_stall(pts_ns)
        fps_cap = self._knobs.temporal.fps_cap
        capped = (
            fps_cap != _NO_CAP
            and pts_ns - self._last_work_pts < 1e9 / fps_cap - _CAP_SLACK_NS
        )
        return stalled or capped

    def _in_stall(self, pts_ns: int) -> bool:
        t = self._knobs.temporal
        if t.stalls_per_min == 0:
            return False
        if self._next_onset is None:
            self._next_onset = pts_ns + self._stall_gap_ns()
        if pts_ns >= self._next_onset:
            # Onsets are a Poisson process on pts time; one that would
            # fall inside a running stall is folded into it.
            self._stall_until = self._next_onset + int(t.stall_len_s * 1e9)
            self._next_onset = self._stall_until + self._stall_gap_ns()
        return self._stall_until is not None and pts_ns < self._stall_until

    def _stall_gap_ns(self) -> int:
        mean_s = 60.0 / self._knobs.temporal.stalls_per_min
        return int(self._rng.exponential(mean_s) * 1e9)

    def _reset_gate(self) -> None:
        self._last_out: bytes | None = None
        self._last_work_pts = 0
        self._next_onset: int | None = None
        self._stall_until: int | None = None

    # --- (2)-(11) pixels ---------------------------------------------------

    def _run_pixels(self, frame: bytes) -> bytes:
        k = self._knobs
        if _pixels_identity(k):
            return frame
        pic, a = k.picture, k.analog
        p = stages.from_nv12(frame, W, H)
        p = stages.downscale(p, pic.width)
        p = self._shutter.apply(p)
        p = stages.tint(p, pic.tint)
        p = stages.colour(p, pic)
        p = stages.chroma_bleed(p, a.chroma_shift, a.chroma_smear)
        p = self._codec.process(p)
        p = stages.upscale(p, pic.upscale)
        if a.interlace:
            p = self._interlace.apply(p)
        p = stages.scanlines(p, a.scanlines)
        p = self._grain.apply(p)
        return stages.to_nv12(p)

    def _new_stages(self) -> None:
        k = self._knobs
        self._shutter = stages.Shutter(k.picture.shutter)
        self._interlace = stages.Interlace()
        self._grain = stages.Grain(k.analog.grain, self._rng)

    def _build_codec(self) -> None:
        """Closes the current codec stage, if any, and builds one for the
        current knobs. On failure the chain is left without one (process
        emits nothing) and ChainConfigError carries the reason."""
        self._close_codec()
        k = self._knobs
        width = k.picture.width
        try:
            self._codec = CodecStage(k.codec, k.temporal.fps_cap, width, width * H // W, self._rng)
        except Exception as e:
            raise ChainConfigError(f"codec {k.codec.kind}: {e}") from e

    def _close_codec(self) -> None:
        codec, self._codec = self._codec, None
        if codec is not None:
            codec.close()

    # --- control -------------------------------------------------------------

    def set_knobs(self, knobs: Knobs) -> None:
        """Keeps the held frame and holding state. Clears the ghost and
        interlace buffers; rebuilds the codec stage only when its own
        knobs, the fps cap or the crushed width change (or the last build
        failed). Raises ChainConfigError if that rebuild fails."""
        old = self._knobs
        self._knobs = knobs
        if knobs.temporal != old.temporal:
            self._next_onset = self._stall_until = None
        self._new_stages()
        if self._codec is None or _codec_key(knobs) != _codec_key(old):
            self._build_codec()

    def hold_grab(self) -> None:
        """Arms the chain; `holding` stays `"empty"` until the next
        `process()` call actually captures a frame."""
        self._holding = _EMPTY
        self._armed = True

    def hold_empty(self) -> None:
        self._holding = _EMPTY
        self._armed = False
        self._held_frame = None

    def release(self) -> None:
        self._holding = _NONE
        self._armed = False
        self._held_frame = None

    def reset(self) -> None:
        """Drops the hold and all temporal state, rebuilding the codec
        stage. Raises ChainConfigError if that rebuild fails."""
        self.release()
        self._reset_gate()
        self._new_stages()
        self._build_codec()

    def close(self) -> None:
        """Releases the codec pipeline. Idempotent."""
        self._close_codec()
