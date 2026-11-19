"""Codec crush stage. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain" row [7] and "Gates" > G2, for the semantics pinned here.
"""
import numpy as np
import pytest

from floficam.filter.codec import CodecStage
from floficam.filter.settings import Codec


def rng(seed):
    return np.random.default_rng(seed)


def _moving_planes(i: int, w: int, h: int):
    """A scrolling bar on a flat background: real, changing inter-frame
    content, so VP8 has residual to encode as deltas and datamosh has
    something to smear. Deterministic (no randomness), so JPEG/VP8
    output comparisons across runs are reproducible."""
    y = np.full((h, w), 100, dtype=np.uint8)
    bar_w = max(8, w // 16)
    x0 = (i * 7) % w
    xs = (np.arange(w) - x0) % w
    y[:, xs < bar_w] = 220
    uv = np.full((h // 2, w // 2, 2), 128, dtype=np.uint8)
    return y, uv


def _mean_abs_diff(a, b) -> float:
    ay, _ = a
    by, _ = b
    return float(np.mean(np.abs(ay.astype(np.int16) - by.astype(np.int16))))


def test_off_is_identity():
    p = _moving_planes(0, 320, 180)
    stage = CodecStage(Codec(), 30, 320, 180, rng(0))
    assert stage.process(p) is p
    stage.close()


def test_jpeg_low_quality_changes_frame_same_shape():
    w, h = 320, 180
    p = _moving_planes(0, w, h)
    stage = CodecStage(Codec(kind="jpeg", jpeg_quality=5), 30, w, h, rng(0))
    try:
        out = stage.process(p)
    finally:
        stage.close()
    y_out, uv_out = out
    y_in, uv_in = p
    assert y_out.shape == y_in.shape
    assert uv_out.shape == uv_in.shape
    assert np.any(y_out != y_in)


def test_vp8_one_output_per_input():
    w, h = 320, 180
    stage = CodecStage(Codec(kind="vp8", vp8_kbps=300), 30, w, h, rng(0))
    try:
        outputs = [stage.process(_moving_planes(i, w, h)) for i in range(60)]
    finally:
        stage.close()
    assert len(outputs) == 60
    for y, uv in outputs:
        assert y.shape == (h, w)
        assert uv.shape == (h // 2, w // 2, 2)


def test_mosh_never_errors_and_reemits():
    w, h = 320, 180
    codec = Codec(kind="vp8", vp8_kbps=300, mosh=0.5)
    stage = CodecStage(codec, 30, w, h, rng(1))
    try:
        outputs = [stage.process(_moving_planes(i, w, h)) for i in range(120)]
    finally:
        stage.close()
    assert len(outputs) == 120
    for y, uv in outputs:
        assert y.shape == (h, w)
        assert uv.shape == (h // 2, w // 2, 2)


def test_mosh_heals_after_keyframe():
    """fps_cap 30 -> keyframe-max-dist 90: a keyframe lands again at
    encoded frame 90. By frame 85 (just before it) the mosh=0.3 stream
    has drifted from the mosh=0 reference (withheld deltas pile up
    stale pixels); by frame 92 (just after the new keyframe) it has
    resynced."""
    w, h = 320, 180
    frames = [_moving_planes(i, w, h) for i in range(95)]

    clean = CodecStage(Codec(kind="vp8", vp8_kbps=300, mosh=0.0), 30, w, h, rng(0))
    moshed = CodecStage(Codec(kind="vp8", vp8_kbps=300, mosh=0.3), 30, w, h, rng(0))
    try:
        clean_out = [clean.process(f) for f in frames]
        moshed_out = [moshed.process(f) for f in frames]
    finally:
        clean.close()
        moshed.close()

    assert _mean_abs_diff(clean_out[85], moshed_out[85]) > 8
    assert _mean_abs_diff(clean_out[92], moshed_out[92]) < 8


def test_keyframe_interval_follows_fps_cap():
    """fps_cap 2 -> keyframe-max-dist 6: with mosh=1.0, every delta
    frame is withheld, so the stage's output only changes on a
    keyframe. The gap between changes must never exceed 6 frames."""
    w, h = 320, 180
    codec = Codec(kind="vp8", vp8_kbps=300, mosh=1.0)
    stage = CodecStage(codec, 2, w, h, rng(0))
    try:
        outputs = [stage.process(_moving_planes(i, w, h)) for i in range(30)]
    finally:
        stage.close()

    changes = [
        i for i in range(1, len(outputs)) if _mean_abs_diff(outputs[i - 1], outputs[i]) > 1
    ]
    assert changes, "expected at least one keyframe to land"
    # outputs[0] is already the first keyframe (frame 0 is always one);
    # each entry in `changes` marks the next keyframe's arrival, so the
    # gap from the previous keyframe is the index itself, not index + 1.
    gaps = [changes[0]] + [b - a for a, b in zip(changes, changes[1:])]
    assert max(gaps) <= 6
