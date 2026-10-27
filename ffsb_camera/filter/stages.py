"""Pure-numpy pixel stages for the effect chain. No GStreamer, no clock:
plain arrays in, plain arrays out, so a later task (the chain) can wire
these into the fixed order without any of them knowing about frame
timing or the pipe protocol. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Effect
chain", for the stage order and the knobs pinned here:

    in 1280x720 -> downscale -> shutter -> tint -> colour+dither ->
    chroma bleed -> (codec crush, Task 7) -> upscale -> interlace ->
    scanlines -> grain -> out 1280x720

`downscale` through `chroma_bleed` run at the crushed (downscaled)
resolution; `upscale` restores 1280x720, and `interlace`/`scanlines`/
`grain` run on that output-resolution frame.

Every stage is a no-op at its "zero" setting (none/true/0/0.0), and most
return the input array unchanged by object identity at that setting --
the thing a caller must not rely on is that a *non-zero* setting never
mutates its input in place; every stage below returns a fresh array
instead.
"""
from __future__ import annotations

import numpy as np

from .settings import Picture

Planes = tuple[np.ndarray, np.ndarray]  # Y (h, w) uint8; UV (h//2, w//2, 2) uint8


# --- NV12 <-> planes <-> RGB ----------------------------------------------


def from_nv12(buf: bytes, w: int, h: int) -> Planes:
    """NV12: `w*h` luma bytes, then an interleaved U,V chroma plane at
    half resolution in each dimension."""
    arr = np.frombuffer(buf, dtype=np.uint8)
    y = arr[: w * h].reshape(h, w).copy()
    uv = arr[w * h :].reshape(h // 2, w // 2, 2).copy()
    return y, uv


def to_nv12(p: Planes) -> bytes:
    y, uv = p
    return np.ascontiguousarray(y, dtype=np.uint8).tobytes() + np.ascontiguousarray(
        uv, dtype=np.uint8
    ).tobytes()


# BT.601 limited-range (studio-swing) conversion matrix -- the same one
# GStreamer's videoconvert uses for SD/"601" content by default.
def nv12_to_rgb(p: Planes) -> np.ndarray:
    y, uv = p
    h, w = y.shape
    u = np.repeat(np.repeat(uv[:, :, 0], 2, axis=0), 2, axis=1)
    v = np.repeat(np.repeat(uv[:, :, 1], 2, axis=0), 2, axis=1)

    c = y.astype(np.float32) - 16.0
    d = u.astype(np.float32) - 128.0
    e = v.astype(np.float32) - 128.0

    r = 1.164 * c + 1.596 * e
    g = 1.164 * c - 0.392 * d - 0.813 * e
    b = 1.164 * c + 2.017 * d

    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(rgb + 0.5, 0, 255).astype(np.uint8)


def rgb_to_planes(rgb: np.ndarray) -> Planes:
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)

    y = 16.0 + 0.257 * r + 0.504 * g + 0.098 * b
    u = 128.0 - 0.148 * r - 0.291 * g + 0.439 * b
    v = 128.0 + 0.439 * r - 0.368 * g - 0.071 * b

    y8 = np.clip(y + 0.5, 0, 255).astype(np.uint8)

    h, w = y8.shape
    u_ds = u.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    v_ds = v.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    uv = np.stack(
        [np.clip(u_ds + 0.5, 0, 255), np.clip(v_ds + 0.5, 0, 255)], axis=-1
    ).astype(np.uint8)
    return y8, uv


# --- resize -----------------------------------------------------------------


def downscale(p: Planes, width: int) -> Planes:
    """Integer reshape-mean. `width` is one of the five crushed widths;
    the height always keeps 16:9, so one integer factor covers both
    planes and both axes."""
    y, uv = p
    h0, w0 = y.shape
    if width == w0:
        return p
    factor = w0 // width
    height = h0 // factor

    y_out = _reshape_mean(y, height, width, factor)
    uh, uw = height // 2, width // 2
    uv_out = np.empty((uh, uw, 2), dtype=np.uint8)
    uv_out[:, :, 0] = _reshape_mean(uv[:, :, 0], uh, uw, factor)
    uv_out[:, :, 1] = _reshape_mean(uv[:, :, 1], uh, uw, factor)
    return y_out, uv_out


def _reshape_mean(plane: np.ndarray, out_h: int, out_w: int, factor: int) -> np.ndarray:
    reshaped = plane.reshape(out_h, factor, out_w, factor).astype(np.float32)
    mean = reshaped.mean(axis=(1, 3))
    return np.clip(mean + 0.5, 0, 255).astype(np.uint8)


def upscale(p: Planes, mode: str) -> Planes:
    """Back to 1280x720. `blocky` is `np.repeat` (nearest); `soft` is
    bilinear, with half-pixel-center sampling and edge clamping."""
    y, uv = p
    h0, w0 = y.shape
    if w0 == 1280:
        return p

    if mode == "blocky":
        factor = 1280 // w0
        y_out = np.repeat(np.repeat(y, factor, axis=0), factor, axis=1)
        uv_out = np.repeat(np.repeat(uv, factor, axis=0), factor, axis=1)
    elif mode == "soft":
        y_out = np.clip(_bilinear(y, 720, 1280) + 0.5, 0, 255).astype(np.uint8)
        uv_out = np.clip(_bilinear(uv, 360, 640) + 0.5, 0, 255).astype(np.uint8)
    else:
        raise ValueError(f"upscale: unknown mode {mode!r}")
    return y_out, uv_out


def _bilinear(arr: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """Bilinear resize of a 2-D or (h, w, c) array to `out_h`x`out_w`,
    float32 out, un-rounded and un-clipped (the caller does both, once,
    after combining with anything else it needs to do)."""
    h, w = arr.shape[0], arr.shape[1]
    scale_y = h / out_h
    scale_x = w / out_w
    ys = np.clip((np.arange(out_h, dtype=np.float32) + 0.5) * scale_y - 0.5, 0, h - 1)
    xs = np.clip((np.arange(out_w, dtype=np.float32) + 0.5) * scale_x - 0.5, 0, w - 1)
    y0 = np.floor(ys).astype(np.intp)
    x0 = np.floor(xs).astype(np.intp)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (ys - y0).astype(np.float32)
    wx = (xs - x0).astype(np.float32)

    af = arr.astype(np.float32)
    if af.ndim == 3:
        wy = wy[:, None, None]
        wx = wx[None, :, None]
    else:
        wy = wy[:, None]
        wx = wx[None, :]

    top = af[y0][:, x0] * (1 - wx) + af[y0][:, x1] * wx
    bot = af[y1][:, x0] * (1 - wx) + af[y1][:, x1] * wx
    return top * (1 - wy) + bot * wy


# --- tint --------------------------------------------------------------

_TINTS: dict[str, tuple[int, int]] = {
    "mono": (128, 128),
    "sepia": (112, 148),
    "cctv": (88, 104),
}


def tint(p: Planes, kind: str) -> Planes:
    """Flat chroma replacement over the whole UV plane; luma untouched."""
    y, uv = p
    if kind == "none":
        return p
    u_val, v_val = _TINTS[kind]
    uv_out = np.empty_like(uv)
    uv_out[:, :, 0] = u_val
    uv_out[:, :, 1] = v_val
    return y, uv_out


# --- palettes ------------------------------------------------------------

_WEB216_LEVELS = np.array([0, 51, 102, 153, 204, 255], dtype=np.uint8)


def _web216_colours() -> np.ndarray:
    r, g, b = np.meshgrid(_WEB216_LEVELS, _WEB216_LEVELS, _WEB216_LEVELS, indexing="ij")
    return np.stack([r, g, b], axis=-1).reshape(-1, 3)


PALETTES: dict[str, np.ndarray] = {
    "2": np.array([[0, 0, 0], [255, 255, 255]], dtype=np.uint8),
    # CGA palette 1, high intensity.
    "cga": np.array(
        [[0, 0, 0], [85, 255, 255], [255, 85, 255], [255, 255, 255]], dtype=np.uint8
    ),
    # DMG greens.
    "gameboy": np.array(
        [[15, 56, 15], [48, 98, 48], [139, 172, 15], [155, 188, 15]], dtype=np.uint8
    ),
    # Standard 16-colour Windows VGA palette.
    "win16": np.array(
        [
            [0, 0, 0],
            [128, 0, 0],
            [0, 128, 0],
            [128, 128, 0],
            [0, 0, 128],
            [128, 0, 128],
            [0, 128, 128],
            [192, 192, 192],
            [128, 128, 128],
            [255, 0, 0],
            [0, 255, 0],
            [255, 255, 0],
            [0, 0, 255],
            [255, 0, 255],
            [0, 255, 255],
            [255, 255, 255],
        ],
        dtype=np.uint8,
    ),
    "web216": _web216_colours(),
}

# Built lazily, once per palette: a 32x32x32 RGB -> nearest-palette-entry
# LUT (8 RGB values per bin), indexed by (R, G, B) // 8.
_PALETTE_LUTS: dict[str, np.ndarray] = {}


def _palette_lut(name: str) -> np.ndarray:
    lut = _PALETTE_LUTS.get(name)
    if lut is not None:
        return lut
    colours = PALETTES[name].astype(np.float32)
    centers = np.arange(32, dtype=np.float32) * 8.0 + 4.0
    gr, gg, gb = np.meshgrid(centers, centers, centers, indexing="ij")
    pts = np.stack([gr, gg, gb], axis=-1).reshape(-1, 3)
    dists = ((pts[:, None, :] - colours[None, :, :]) ** 2).sum(axis=2)
    nearest = np.argmin(dists, axis=1)
    lut = colours[nearest].reshape(32, 32, 32, 3).astype(np.uint8)
    _PALETTE_LUTS[name] = lut
    return lut


def _apply_palette(rgb: np.ndarray, name: str) -> np.ndarray:
    lut = _palette_lut(name)
    idx = np.clip((rgb / 8.0).astype(np.intp), 0, 31)
    return lut[idx[..., 0], idx[..., 1], idx[..., 2]]


# --- ordered (Bayer) dither ------------------------------------------------

_BAYER_CACHE: dict[int, np.ndarray] = {}


def _bayer_matrix(n: int) -> np.ndarray:
    m = _BAYER_CACHE.get(n)
    if m is not None:
        return m
    if n == 1:
        m = np.zeros((1, 1), dtype=np.int32)
    else:
        s = _bayer_matrix(n // 2)
        m = np.block([[4 * s, 4 * s + 2], [4 * s + 3, 4 * s + 1]])
    _BAYER_CACHE[n] = m
    return m


def _bayer_tile(size: int, h: int, w: int) -> np.ndarray:
    """A `size`x`size` ordered-dither threshold map, normalized to
    roughly [-0.5, 0.5) and tiled to `h`x`w`."""
    m = _bayer_matrix(size).astype(np.float32)
    norm = (m + 0.5) / (size * size) - 0.5
    reps_h = -(-h // size)
    reps_w = -(-w // size)
    return np.tile(norm, (reps_h, reps_w))[:h, :w]


# --- colour depth --------------------------------------------------------


def colour(p: Planes, pic: Picture) -> Planes:
    """NV12 -> RGB -> (posterize | palette quantization, optionally
    ordered-dithered) -> NV12. A no-op at `colour="true"` (dither is
    irrelevant then, and ignored)."""
    if pic.colour == "true":
        return p

    rgb = nv12_to_rgb(p).astype(np.float32)
    h, w = rgb.shape[:2]
    web216 = pic.colour == "palette" and pic.palette == "web216"

    if pic.dither != "none":
        size = 4 if pic.dither == "bayer4" else 8
        if pic.colour == "posterize":
            step = 255.0 / (pic.levels - 1)
        elif web216:
            step = 255.0 / 5.0
        else:
            # Small LUT palettes (2/cga/gameboy/win16): roughly
            # 1/palette-size of the full range. Picked and pinned here
            # rather than derived per palette, since the palettes are
            # irregularly spaced -- 48 reads as a visible dither grain
            # without washing the (few) colours out.
            step = 48.0
        rgb = rgb + _bayer_tile(size, h, w)[:, :, None] * step

    if pic.colour == "posterize":
        step = 255.0 / (pic.levels - 1)
        rgb_out = np.clip(np.round(rgb / step) * step, 0, 255).astype(np.uint8)
    elif web216:
        step = 255.0 / 5.0
        rgb_out = np.clip(np.round(rgb / step) * step, 0, 255).astype(np.uint8)
    else:
        rgb_out = _apply_palette(np.clip(rgb, 0, 255), pic.palette)

    return rgb_to_planes(rgb_out)


# --- chroma bleed ----------------------------------------------------------


def chroma_bleed(p: Planes, shift: int, smear: int) -> Planes:
    """Horizontal shift (edge-clamped) plus a horizontal box blur, both
    on the UV plane only."""
    y, uv = p
    if shift == 0 and smear == 0:
        return p

    out = uv
    if shift:
        h, w = out.shape[:2]
        padded = np.pad(out, ((0, 0), (shift, 0), (0, 0)), mode="edge")
        out = padded[:, :w, :]
    if smear:
        out = _box_blur_horizontal(out, smear)
    return y, out


def _box_blur_horizontal(uv: np.ndarray, smear: int) -> np.ndarray:
    k = 2 * smear + 1
    h, w, c = uv.shape
    padded = np.pad(uv.astype(np.float32), ((0, 0), (smear, smear), (0, 0)), mode="edge")
    csum = np.cumsum(padded, axis=1, dtype=np.float32)
    csum = np.concatenate([np.zeros((h, 1, c), dtype=np.float32), csum], axis=1)
    out = (csum[:, k:, :] - csum[:, :-k, :]) / k
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


# --- scanlines ---------------------------------------------------------


def scanlines(p: Planes, strength: float) -> Planes:
    """Darkens odd luma rows (output resolution, after upscale); chroma
    untouched."""
    y, uv = p
    if strength == 0.0:
        return p
    y_out = y.copy()
    darkened = y_out[1::2, :].astype(np.float32) * (1.0 - strength)
    y_out[1::2, :] = np.clip(darkened + 0.5, 0, 255).astype(np.uint8)
    return y_out, uv


# --- stateful stages (hold reset()) -----------------------------------


class Shutter:
    """Ghosting: an exponential moving average of past outputs, so old
    frames decay rather than vanish outright. `out = p*prev + (1-p)*cur`,
    `prev` being the previous call's *output* (the accumulator), not its
    input. The first call (or any call at `persistence == 0`) returns
    `cur` unchanged."""

    def __init__(self, persistence: float) -> None:
        self._persistence = persistence
        self._prev: Planes | None = None

    def apply(self, p: Planes) -> Planes:
        y, uv = p
        if self._prev is None or self._persistence == 0.0:
            out = (y.copy(), uv.copy())
        else:
            py, puv = self._prev
            pers = self._persistence
            yf = pers * py.astype(np.float32) + (1 - pers) * y.astype(np.float32)
            uvf = pers * puv.astype(np.float32) + (1 - pers) * uv.astype(np.float32)
            out = (
                np.clip(yf + 0.5, 0, 255).astype(np.uint8),
                np.clip(uvf + 0.5, 0, 255).astype(np.uint8),
            )
        self._prev = out
        return out

    def reset(self) -> None:
        self._prev = None


class Interlace:
    """Combing: odd output rows come from the *previous call's input*;
    even rows are the current input. The first call has no previous
    input, so it returns the current input unchanged."""

    def __init__(self) -> None:
        self._prev_input: Planes | None = None

    def apply(self, p: Planes) -> Planes:
        y, uv = p
        if self._prev_input is None:
            out = (y.copy(), uv.copy())
        else:
            py, puv = self._prev_input
            y_out = y.copy()
            y_out[1::2, :] = py[1::2, :]
            uv_out = uv.copy()
            uv_out[1::2, :, :] = puv[1::2, :, :]
            out = (y_out, uv_out)
        self._prev_input = (y.copy(), uv.copy())
        return out

    def reset(self) -> None:
        self._prev_input = None


class Grain:
    """Luma noise from a precomputed 256x256 tile, rolled by a random
    offset each call and tiled to cover the frame. A no-op at
    `amount == 0`."""

    _TILE_SIZE = 256
    _TILE_RANGE = 64  # tile values in [-64, 64]; `amount` scales it down

    def __init__(self, amount: float, rng) -> None:
        self._amount = amount
        self._rng = rng
        self._tile = rng.integers(
            -self._TILE_RANGE,
            self._TILE_RANGE + 1,
            size=(self._TILE_SIZE, self._TILE_SIZE),
            dtype=np.int16,
        )

    def apply(self, p: Planes) -> Planes:
        y, uv = p
        if self._amount == 0.0:
            return p
        h, w = y.shape
        off_y = int(self._rng.integers(0, self._TILE_SIZE))
        off_x = int(self._rng.integers(0, self._TILE_SIZE))
        tile = np.roll(self._tile, (off_y, off_x), axis=(0, 1))
        reps_h = -(-h // self._TILE_SIZE)
        reps_w = -(-w // self._TILE_SIZE)
        noise = np.tile(tile, (reps_h, reps_w))[:h, :w].astype(np.float32)
        y_out = np.clip(
            y.astype(np.float32) + noise * self._amount + 0.5, 0, 255
        ).astype(np.uint8)
        return y_out, uv

    def reset(self) -> None:
        """No persistent state to drop: each call already draws a fresh
        offset. Kept for interface symmetry with Shutter/Interlace."""
