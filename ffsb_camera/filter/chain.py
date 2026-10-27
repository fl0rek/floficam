"""The effect chain: one call per input frame, with real hold modes and
(for now) an identity pixel path. Tasks 5-7 slot the stages and the
fps/stall gate in between the hold check and the identity return. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain" stage (1) and "The held flag", for the semantics pinned here.

Holding: `HOLD_GRAB` arms the chain to grab its *next* input as the held
frame (so a `HOLD_GRAB` that races a live frame never captures live
pixels after the fact); every call after that runs the full chain on the
held frame and flags the output. Between `HOLD_GRAB` and that next call,
nothing is held yet, so `holding` reports `"empty"`, same as `HOLD_EMPTY`
-- it flips to `"frame"` only once the grab actually captures a frame.
`HOLD_EMPTY` emits nothing at all, and never captures. `RELEASE` and
`reset()` both drop back to live; `reset()` additionally clears whatever
temporal/codec state later tasks add. `set_knobs` never touches hold
state -- a settings reload must not un-pause a frozen call.
"""
from __future__ import annotations

from dataclasses import dataclass

from .settings import Knobs

_NONE = "none"
_EMPTY = "empty"
_FRAME = "frame"


@dataclass(frozen=True)
class Output:
    frame: bytes
    held: bool


class Chain:
    def __init__(self, knobs: Knobs, rng) -> None:
        self._knobs = knobs
        self._rng = rng
        self._holding = _NONE
        self._armed = False  # HOLD_GRAB issued, waiting for the next input
        self._held_frame: bytes | None = None

    @property
    def holding(self) -> str:
        return self._holding

    def process(self, frame: bytes, pts_ns: int) -> Output | None:
        if self._armed:
            # The grab: capture this input as the held frame. Checked
            # before the `_EMPTY` early-return below, since `hold_grab`
            # reports "empty" (nothing held yet) right up to this call.
            self._held_frame = frame
            self._armed = False
            self._holding = _FRAME
        elif self._holding == _EMPTY:
            return None

        if self._holding == _FRAME:
            frame = self._held_frame

        out = self._run_pixels(frame)
        return Output(out, self._holding == _FRAME)

    def _run_pixels(self, frame: bytes) -> bytes:
        """The identity path. Stages 2-11 land here in Tasks 5-7."""
        return frame

    def set_knobs(self, knobs: Knobs) -> None:
        """Keeps the held frame and holding state; only the pixel-path
        knobs change."""
        self._knobs = knobs

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
        """Drops the held frame and all temporal state (later tasks add
        the ghost buffer, codec pipeline, etc. here)."""
        self.release()

    def close(self) -> None:
        """Releases the codec pipeline (Task 7). A no-op until then;
        idempotent."""
