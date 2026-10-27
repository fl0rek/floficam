"""Pure-numpy pixel stages. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain", for the stage order and semantics pinned here.
"""
import numpy as np
import pytest

from ffsb_camera.filter import stages
from ffsb_camera.filter.settings import Picture

W, H = 1280, 720
FRAME_SIZE = W * H * 3 // 2


def _random_frame(seed=0) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=FRAME_SIZE, dtype=np.uint8).tobytes()


def _random_planes(seed=0, w=W, h=H):
    rng = np.random.default_rng(seed)
    y = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
    uv = rng.integers(0, 256, size=(h // 2, w // 2, 2), dtype=np.uint8)
    return y, uv


def _assert_planes_equal(a, b):
    ay, auv = a
    by, buv = b
    assert np.array_equal(ay, by)
    assert np.array_equal(auv, buv)


def _smooth_planes(seed=0, w=W, h=H, block=16):
    """A self-consistent NV12 frame built from flat colour blocks (via a
    real RGB -> planes conversion), rather than independently-randomized
    Y and UV. Colour-quantization tests need this: per-pixel-random Y/UV
    is not a frame any camera (or this module's own rgb_to_planes) would
    ever produce, and its 4:2:0 chroma subsampling smears discrete
    palette/posterize levels across neighbours in a way a real frame's
    locally-coherent colour never would."""
    rng = np.random.default_rng(seed)
    bh, bw = h // block, w // block
    block_colours = rng.integers(0, 256, size=(bh, bw, 3), dtype=np.uint8)
    rgb = np.repeat(np.repeat(block_colours, block, axis=0), block, axis=1)
    return stages.rgb_to_planes(rgb)


# --- NV12 roundtrip ----------------------------------------------------


def test_roundtrip_nv12():
    buf = _random_frame()
    assert stages.to_nv12(stages.from_nv12(buf, W, H)) == buf


def test_rgb_planes_roundtrip():
    """rgb_to_planes -> nv12_to_rgb recovers each channel within ±2 --
    the BT.601 forward/inverse matrices are standard but not exact
    inverses at finite precision; this is the accuracy budget the
    colour stage's quantization tests below rely on."""
    rgb = np.repeat(
        np.repeat(np.random.default_rng(3).integers(0, 256, size=(45, 80, 3), dtype=np.uint8), 16, axis=0),
        16,
        axis=1,
    )
    back = stages.nv12_to_rgb(stages.rgb_to_planes(rgb))
    assert np.abs(rgb.astype(np.int16) - back.astype(np.int16)).max() <= 2


# --- identity at zero setting --------------------------------------------


def test_identity_at_zero():
    p = _random_planes()

    _assert_planes_equal(stages.tint(p, "none"), p)
    _assert_planes_equal(stages.colour(p, Picture(colour="true")), p)
    _assert_planes_equal(stages.chroma_bleed(p, 0, 0), p)
    _assert_planes_equal(stages.scanlines(p, 0.0), p)
    _assert_planes_equal(stages.downscale(p, 1280), p)
    _assert_planes_equal(stages.upscale(p, "blocky"), p)
    _assert_planes_equal(stages.upscale(p, "soft"), p)

    shutter = stages.Shutter(0.0)
    _assert_planes_equal(shutter.apply(p), p)
    _assert_planes_equal(shutter.apply(_random_planes(seed=1)), _random_planes(seed=1))

    rng = np.random.default_rng(1)
    grain = stages.Grain(0.0, rng)
    _assert_planes_equal(grain.apply(p), p)


# --- downscale / upscale -------------------------------------------------


def test_downscale_dims():
    p = _random_planes()
    y, uv = stages.downscale(p, 320)
    assert y.shape == (180, 320)
    assert uv.shape == (90, 160, 2)


@pytest.mark.parametrize("width,height", [(640, 360), (320, 180), (256, 144), (160, 90)])
def test_downscale_all_widths(width, height):
    p = _random_planes()
    y, uv = stages.downscale(p, width)
    assert y.shape == (height, width)
    assert uv.shape == (height // 2, width // 2, 2)


@pytest.mark.parametrize("mode", ["blocky", "soft"])
def test_upscale_back_to_1280x720(mode):
    p = _random_planes(w=320, h=180)
    y, uv = stages.upscale(p, mode)
    assert y.shape == (720, 1280)
    assert uv.shape == (360, 640, 2)


# --- colour quantization ---------------------------------------------------


def test_palette_only_palette_colours():
    p = _smooth_planes()
    out = stages.colour(p, Picture(colour="palette", palette="gameboy"))
    rgb = stages.nv12_to_rgb(out).astype(np.int16)
    entries = stages.PALETTES["gameboy"].astype(np.int16)
    dists = np.abs(rgb[:, :, None, :] - entries[None, None, :, :])
    nearest_diff = dists.min(axis=2)
    assert nearest_diff.max() <= 2


def _assert_near_levels(rgb, levels, tolerance=2):
    """Every channel of every pixel lands within `tolerance` of one of
    `levels` -- the YUV<->RGB roundtrip (±1, see test_rgb_planes_roundtrip
    below) stacks with the quantization step itself, so an exact
    np.unique count is the wrong check; distance to the nearest legal
    level is what the knob actually promises."""
    rgb = rgb.astype(np.int16)
    levels = np.asarray(levels, dtype=np.int16)
    nearest = levels[np.argmin(np.abs(rgb[..., None] - levels), axis=-1)]
    assert np.abs(rgb - nearest).max() <= tolerance


def test_web216_levels():
    p = _smooth_planes()
    out = stages.colour(p, Picture(colour="palette", palette="web216"))
    rgb = stages.nv12_to_rgb(out)
    _assert_near_levels(rgb, [0, 51, 102, 153, 204, 255])


def test_posterize_levels():
    p = _smooth_planes()
    out = stages.colour(p, Picture(colour="posterize", levels=4))
    rgb = stages.nv12_to_rgb(out)
    _assert_near_levels(rgb, [0, 85, 170, 255])


# --- shutter / interlace / grain -----------------------------------------


def test_shutter_converges():
    shutter = stages.Shutter(0.5)
    a = _random_planes(seed=1)
    b = _random_planes(seed=2)
    shutter.apply(a)
    out = b
    for _ in range(40):
        out = shutter.apply(b)
    ay, auv = out
    by, buv = b
    assert np.abs(ay.astype(np.int16) - by.astype(np.int16)).max() <= 1
    assert np.abs(auv.astype(np.int16) - buv.astype(np.int16)).max() <= 1


def test_interlace_combs():
    il = stages.Interlace()
    f1 = _random_planes(seed=1)
    f2 = _random_planes(seed=2)
    il.apply(f1)
    y_out, uv_out = il.apply(f2)

    y1, uv1 = f1
    y2, uv2 = f2
    assert np.array_equal(y_out[1::2, :], y1[1::2, :])
    assert np.array_equal(y_out[0::2, :], y2[0::2, :])
    assert np.array_equal(uv_out[1::2, :, :], uv1[1::2, :, :])
    assert np.array_equal(uv_out[0::2, :, :], uv2[0::2, :, :])


def test_grain_deterministic():
    p = _random_planes()
    rng1 = np.random.default_rng(42)
    rng2 = np.random.default_rng(42)
    out1 = stages.Grain(1.0, rng1).apply(p)
    out2 = stages.Grain(1.0, rng2).apply(p)
    _assert_planes_equal(out1, out2)
    # And it actually perturbs the frame.
    assert not np.array_equal(out1[0], p[0])


def test_scanlines_darken_odd_rows_only():
    y, uv = _random_planes()
    y = np.clip(y.astype(np.int16) + 50, 0, 255).astype(np.uint8)  # avoid floor clipping
    p = (y, uv)
    y_out, uv_out = stages.scanlines(p, 0.5)
    assert np.array_equal(y_out[0::2, :], y[0::2, :])
    assert np.all(y_out[1::2, :].astype(np.int16) < y[1::2, :].astype(np.int16))
    assert np.array_equal(uv_out, uv)
