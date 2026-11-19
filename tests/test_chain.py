"""The effect chain: hold modes, the temporal gate (fps cap, stalls) and
the pixel stages in their fixed order. The short byte-string frames
(A/B/C) only work while every pixel knob is at its zero setting, where
the chain passes bytes through untouched; everything else runs on real
1280x720 NV12. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain", "The held flag", and "Testing".
"""
from dataclasses import replace

import numpy as np
import pytest

from floficam.filter import bench, chain as chain_mod
from floficam.filter.chain import Chain, ChainConfigError, Output
from floficam.filter.codec import CodecStage
from floficam.filter.settings import (
    Analog,
    Codec,
    Knobs,
    Picture,
    Temporal,
    builtin_presets_path,
    load_presets,
)

A = b"A" * 100
B = b"B" * 100
C = b"C" * 100


W, H = 1280, 720
FRAME_NS = 1_000_000_000 // 30

P, _ = load_presets(builtin_presets_path(), None)


def rng(seed):
    return np.random.default_rng(seed)


def frame(i: int) -> bytes:
    """Real 1280x720 NV12 with content that changes every frame: a
    diagonal gradient scrolled by `i`, plus a bright block that moves, on
    a chroma ramp. Distinct per `i` at every crushed width."""
    yy, xx = np.mgrid[0:H, 0:W]
    y = ((xx + yy + 9 * i) % 200 + 16).astype(np.uint8)
    bx, by = (40 * i) % (W - 160), (24 * i) % (H - 160)
    y[by : by + 160, bx : bx + 160] = 235
    uv = np.empty((H // 2, W // 2, 2), dtype=np.uint8)
    uv[:, :, 0] = (np.arange(W // 2) * 200 // (W // 2) + 28)[None, :]
    uv[:, :, 1] = ((np.arange(H // 2) + 5 * i) % 200 + 28)[:, None]
    return y.tobytes() + uv.tobytes()


def pts(i: int) -> int:
    return i * FRAME_NS


def full_chain(knobs: Knobs, f: bytes) -> bytes:
    """What a fresh chain makes of `f` (the first frame always runs)."""
    c = Chain(knobs, rng(0))
    try:
        return c.process(f, 0).frame
    finally:
        c.close()


def test_identity_without_hold():
    c = Chain(Knobs(), rng(0))
    assert c.process(A, 0) == Output(A, False)
    assert c.holding == "none"


def test_hold_grab_holds_next_input_and_flags():
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.hold_grab()
    o1 = c.process(B, 1)
    o2 = c.process(C, 2)
    assert o1 == o2 == Output(B, True)
    assert c.holding == "frame"


def test_hold_grab_reports_empty_until_captured():
    """R6: between HOLD_GRAB and the input that gets captured, nothing is
    held yet, so `holding` stays "empty"; it flips to "frame" only once
    that next process() call actually captures a frame."""
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.hold_grab()
    assert c.holding == "empty"
    c.process(B, 1)
    assert c.holding == "frame"


def test_hold_empty_emits_nothing():
    c = Chain(Knobs(), rng(0))
    c.hold_empty()
    assert c.process(A, 0) is None
    assert c.holding == "empty"


def test_release_back_to_live():
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.hold_grab()
    c.process(B, 1)
    c.release()
    assert c.holding == "none"
    assert c.process(C, 3) == Output(C, False)


def test_set_knobs_keeps_hold():
    c = Chain(Knobs(), rng(0))
    c.hold_grab()
    c.process(B, 1)
    c.set_knobs(Knobs(analog=Analog(grain=0.0)))
    assert c.process(C, 2) == Output(B, True)
    assert c.holding == "frame"


def test_reset_drops_hold():
    c = Chain(Knobs(), rng(0))
    c.hold_grab()
    c.process(B, 1)
    c.reset()
    assert c.holding == "none"
    assert c.process(C, 2) == Output(C, False)


def test_close_is_idempotent():
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.close()
    c.close()


# --- the full chain ---------------------------------------------------------


@pytest.mark.parametrize("pid", [p for p in P if p != "none"])
def test_every_builtin_changes_frame(pid):
    c = Chain(P[pid].knobs, rng(0))
    f = frame(3)
    out = c.process(f, 0)
    c.close()
    assert out.held is False
    assert len(out.frame) == len(f)
    assert out.frame != f


def test_none_preset_is_identity():
    c = Chain(P["none"].knobs, rng(0))
    for i in range(4):
        f = frame(i)
        assert c.process(f, pts(i)) == Output(f, False)
    c.close()


def test_stage_order_codec_after_colour():
    """Codec crush runs on the colour-quantized picture: a palette fed
    into JPEG comes out with colours that are no longer all in the
    palette, which the other order would never produce."""
    pic = Picture(width=320, colour="palette", palette="gameboy")
    quantized = full_chain(Knobs(picture=pic), frame(1))
    crushed = full_chain(Knobs(picture=pic, codec=Codec(kind="jpeg", jpeg_quality=5)), frame(1))
    assert crushed != quantized


# --- the temporal gate --------------------------------------------------------


def test_fps_cap_repeats_last_output():
    knobs = Knobs(temporal=Temporal(fps_cap=5), picture=Picture(width=320))
    c = Chain(knobs, rng(0))
    outs = [c.process(frame(i), pts(i)) for i in range(7)]
    c.close()
    assert all(o == outs[0] for o in outs[:6])
    assert outs[6].frame != outs[0].frame
    assert outs[6].frame == full_chain(knobs, frame(6))


def test_fps_cap_30_is_off():
    """30 is the cap's zero setting: every input runs, even ones closer
    together than a 30 fps frame interval."""
    knobs = Knobs(picture=Picture(width=320))
    c = Chain(knobs, rng(0))
    outs = [c.process(frame(i), i) for i in range(3)]
    c.close()
    assert len({o.frame for o in outs}) == 3


def test_hold_grab_bypasses_fps_cap():
    knobs = Knobs(temporal=Temporal(fps_cap=1), picture=Picture(width=320, tint="mono"))
    c = Chain(knobs, rng(0))
    a = c.process(frame(0), pts(0))
    c.hold_grab()
    b = c.process(frame(1), pts(1))
    c.close()
    assert b.held is True
    assert b.frame != a.frame
    assert b.frame == full_chain(knobs, frame(1))


def test_held_reemits_under_the_cap_stay_flagged():
    knobs = Knobs(temporal=Temporal(fps_cap=1), picture=Picture(width=320))
    c = Chain(knobs, rng(0))
    c.process(frame(0), pts(0))
    c.hold_grab()
    held = c.process(frame(1), pts(1))
    again = c.process(frame(2), pts(2))
    c.close()
    assert again == held == Output(full_chain(knobs, frame(1)), True)


def _find_stall(c: Chain, limit: int) -> tuple[int, Output]:
    """Feeds distinct frames at 30 fps until an output repeats the one
    before it -- the fps cap is off, so that is a stall -- and returns
    the first stalled frame's index and output. The stall's onset lies in
    (pts(s - 1), pts(s)]."""
    prev = c.process(frame(0), pts(0))
    for i in range(1, limit):
        out = c.process(frame(i), pts(i))
        if out == prev:
            return i, out
        prev = out
    raise AssertionError(f"no stall within {limit} frames")


def test_stalls_repeat_for_their_length():
    knobs = Knobs(
        temporal=Temporal(stalls_per_min=20, stall_len_s=1.0), picture=Picture(width=160)
    )
    c = Chain(knobs, rng(1))
    s, stalled = _find_stall(c, 30 * 60)
    # Onset in (pts(s-1), pts(s)], 1 s long: s..s+29 are inside it for
    # certain, s+31 onwards are past it.
    inside = [c.process(frame(j), pts(j)) for j in range(s + 1, s + 30)]
    c.process(frame(s + 30), pts(s + 30))
    after = [c.process(frame(j), pts(j)) for j in range(s + 31, s + 36)]
    c.close()
    assert all(o == stalled for o in inside)
    assert any(o != stalled for o in after)


def test_hold_grab_mid_stall():
    knobs = Knobs(
        temporal=Temporal(stalls_per_min=20, stall_len_s=3.0),
        picture=Picture(width=320, tint="mono"),
    )
    c = Chain(knobs, rng(1))
    s, stalled = _find_stall(c, 30 * 60)
    assert c.process(frame(s + 1), pts(s + 1)) == stalled
    c.hold_grab()
    held = c.process(frame(s + 2), pts(s + 2))
    c.close()
    assert held.held is True
    assert held.frame != stalled.frame
    assert held.frame == full_chain(knobs, frame(s + 2))


def test_held_grain_keeps_moving():
    knobs = Knobs(picture=Picture(width=320), analog=Analog(grain=0.3))
    c = Chain(knobs, rng(0))
    c.process(frame(0), pts(0))
    c.hold_grab()
    outs = [c.process(frame(i), pts(i)) for i in range(1, 5)]
    c.close()
    assert all(o.held for o in outs)
    assert len({o.frame for o in outs}) == 4


def test_set_knobs_codec_change_keeps_hold():
    plain = Knobs(picture=Picture(width=320))
    jpeg = replace(plain, codec=Codec(kind="jpeg", jpeg_quality=5))
    c = Chain(plain, rng(0))
    c.process(frame(0), pts(0))
    c.hold_grab()
    c.process(frame(1), pts(1))
    c.set_knobs(jpeg)
    out = c.process(frame(2), pts(2))
    c.close()
    assert c.holding == "frame"
    assert out == Output(full_chain(jpeg, frame(1)), True)


def test_reset_clears_temporal_state():
    """Ghosting state and the gate's last output go with a reset: the
    first frame after it runs fresh, as if on a new chain."""
    knobs = Knobs(temporal=Temporal(fps_cap=1), picture=Picture(width=320, shutter=0.8))
    c = Chain(knobs, rng(0))
    for i in range(3):
        c.process(frame(i), pts(i))
    c.reset()
    out = c.process(frame(5), pts(5))
    c.close()
    assert out == Output(full_chain(knobs, frame(5)), False)


def test_set_knobs_clears_shutter():
    ghost = Knobs(picture=Picture(width=320, shutter=0.9))
    c = Chain(ghost, rng(0))
    c.process(frame(0), pts(0))
    c.set_knobs(ghost)
    out = c.process(frame(1), pts(1))
    c.close()
    assert out.frame == full_chain(ghost, frame(1))


# --- codec stage lifetime (R12) -------------------------------------------------


class _RecordingCodec:
    """Stands in for CodecStage: records construction and close."""

    made: list = []

    def __init__(self, codec, fps_cap, w, h, rng):
        self.codec, self.fps_cap, self.size = codec, fps_cap, (w, h)
        self.closed = False
        _RecordingCodec.made.append(self)

    def process(self, p):
        return p

    def close(self):
        self.closed = True


@pytest.fixture
def recording_codec(monkeypatch):
    _RecordingCodec.made = []
    monkeypatch.setattr(chain_mod, "CodecStage", _RecordingCodec)
    return _RecordingCodec.made


def test_codec_rebuilt_only_on_codec_fps_or_width_change(recording_codec):
    base = Knobs(picture=Picture(width=320), codec=Codec(kind="jpeg"))
    c = Chain(base, rng(0))
    assert len(recording_codec) == 1 and recording_codec[0].size == (320, 180)

    c.set_knobs(replace(base, analog=Analog(grain=0.2)))
    assert len(recording_codec) == 1

    c.set_knobs(replace(base, codec=Codec(kind="jpeg", jpeg_quality=10)))
    assert len(recording_codec) == 2 and recording_codec[0].closed

    c.set_knobs(replace(base, codec=Codec(kind="jpeg", jpeg_quality=10), temporal=Temporal(fps_cap=5)))
    assert len(recording_codec) == 3 and recording_codec[1].closed
    assert recording_codec[2].fps_cap == 5

    c.set_knobs(
        replace(
            base,
            codec=Codec(kind="jpeg", jpeg_quality=10),
            temporal=Temporal(fps_cap=5),
            picture=Picture(width=160),
        )
    )
    assert len(recording_codec) == 4 and recording_codec[3].size == (160, 90)

    c.close()
    c.close()
    assert all(s.closed for s in recording_codec)


def test_reset_rebuilds_codec(recording_codec):
    c = Chain(Knobs(codec=Codec(kind="vp8")), rng(0))
    c.reset()
    assert len(recording_codec) == 2 and recording_codec[0].closed
    c.close()


def _failing_codec(monkeypatch):
    def fail(codec, fps_cap, w, h, rng):
        if codec.kind != "off":
            raise RuntimeError("no element \"vp8enc\"")
        return CodecStage(codec, fps_cap, w, h, rng)

    monkeypatch.setattr(chain_mod, "CodecStage", fail)


def test_codec_construction_failure_fails_closed(monkeypatch):
    _failing_codec(monkeypatch)
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    with pytest.raises(ChainConfigError, match="vp8enc"):
        c.set_knobs(Knobs(codec=Codec(kind="vp8")))
    assert c.process(B, 1) is None
    c.set_knobs(Knobs())
    assert c.process(C, 2) == Output(C, False)
    c.close()


def test_codec_construction_failure_at_init_raises(monkeypatch):
    _failing_codec(monkeypatch)
    with pytest.raises(ChainConfigError, match="vp8enc"):
        Chain(Knobs(codec=Codec(kind="vp8")), rng(0))



def test_codec_rebuild_failure_on_reset_fails_closed(monkeypatch):
    c = Chain(Knobs(codec=Codec(kind="jpeg")), rng(0))
    _failing_codec(monkeypatch)
    with pytest.raises(ChainConfigError, match="vp8enc"):
        c.reset()
    assert c.process(A, 0) is None
    c.close()


# --- bench --------------------------------------------------------------------


def test_bench_runs(capsys):
    assert bench.main(["--frames", "5", "--max-ms", "100000"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == list(P)
    for line in lines:
        float(line.split()[1])


def test_bench_fails_over_budget(capsys):
    assert bench.main(["--frames", "2", "--max-ms", "0"]) == 1
