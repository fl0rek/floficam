"""A minimal PNG encoder for the Control Panel preview helper
(`preview.py`): 8-bit truecolour, no interlacing, filter type 0 (None)
on every scanline, zlib level 1 -- speed over size, since every frame
this writes is replaced or deleted within a few more. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"Preview".
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COLOUR_TYPE_RGB = 2
_BIT_DEPTH = 8
_ZLIB_LEVEL = 1


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def encode_rgb(rgb: np.ndarray) -> bytes:
    """`rgb`: an (h, w, 3) uint8 array. Each scanline gets a leading
    filter-type byte (0) before the whole image is deflated in one go, as
    PNG's scanline filtering requires."""
    h, w = rgb.shape[0], rgb.shape[1]
    ihdr = struct.pack(">IIBBBBB", w, h, _BIT_DEPTH, _COLOUR_TYPE_RGB, 0, 0, 0)

    filtered = np.zeros((h, w * 3 + 1), dtype=np.uint8)
    filtered[:, 1:] = np.ascontiguousarray(rgb, dtype=np.uint8).reshape(h, w * 3)
    idat = zlib.compress(filtered.tobytes(), _ZLIB_LEVEL)

    return _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
