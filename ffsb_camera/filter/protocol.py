"""Length-prefixed binary messages between the feeder and the filter
child (`ffsb-camera-filter`). See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
filter child" > "Protocol", for the message table, and the plan's Global
Constraints for the type bytes and struct formats pinned here.

Every message is a one-byte type, a u32 payload length (little-endian,
`struct "<BI"`), then the payload. `FRAME` is one type byte shared by both
directions; its payload shape differs by direction (`decode_frame_in` vs
`decode_frame_out`), so callers must know which side of the pipe they are
reading, same as the design calls for.
"""
from __future__ import annotations

import json
import struct
from typing import BinaryIO

FRAME = 1
HOLD_GRAB = 2
HOLD_EMPTY = 3
RELEASE = 4
RESET = 5
STATUS = 6

_HEADER = struct.Struct("<BI")
_FRAME_IN_HDR = struct.Struct("<IQ")
_FRAME_OUT_HDR = struct.Struct("<IQB")
_RESET_PAYLOAD = struct.Struct("<I")

_HELD_BIT = 0x01


class ProtocolError(Exception):
    """A message was truncated at EOF: a partial header or a header whose
    payload never fully arrived."""


def _message(kind: int, payload: bytes) -> bytes:
    return _HEADER.pack(kind, len(payload)) + payload


def encode_frame_in(epoch: int, pts_ns: int, frame: bytes) -> bytes:
    """feeder -> child."""
    return _message(FRAME, _FRAME_IN_HDR.pack(epoch, pts_ns) + frame)


def encode_frame_out(epoch: int, pts_ns: int, held: bool, frame: bytes) -> bytes:
    """child -> feeder."""
    flags = _HELD_BIT if held else 0
    return _message(FRAME, _FRAME_OUT_HDR.pack(epoch, pts_ns, flags) + frame)


def encode_control(kind: int) -> bytes:
    """HOLD_GRAB, HOLD_EMPTY or RELEASE: no payload."""
    return _message(kind, b"")


def encode_reset(epoch: int) -> bytes:
    return _message(RESET, _RESET_PAYLOAD.pack(epoch))


def encode_status(d: dict) -> bytes:
    return _message(STATUS, json.dumps(d).encode("utf-8"))


def decode_frame_in(payload: bytes) -> tuple[int, int, bytes]:
    epoch, pts_ns = _FRAME_IN_HDR.unpack_from(payload)
    return epoch, pts_ns, payload[_FRAME_IN_HDR.size :]


def decode_frame_out(payload: bytes) -> tuple[int, int, bool, bytes]:
    epoch, pts_ns, flags = _FRAME_OUT_HDR.unpack_from(payload)
    held = bool(flags & _HELD_BIT)
    return epoch, pts_ns, held, payload[_FRAME_OUT_HDR.size :]


class Parser:
    """Incremental framing over a byte stream: `feed` returns every
    complete message in `data`, buffering the rest for the next call."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(data)
        out: list[tuple[int, bytes]] = []
        while True:
            if len(self._buf) < _HEADER.size:
                break
            kind, length = _HEADER.unpack_from(self._buf)
            end = _HEADER.size + length
            if len(self._buf) < end:
                break
            payload = bytes(self._buf[_HEADER.size : end])
            del self._buf[:end]
            out.append((kind, payload))
        return out


def read_message(f: BinaryIO) -> tuple[int, bytes] | None:
    """A blocking convenience for tests and the feeder side: reads exactly
    one message from `f`. `None` on a clean EOF before any byte of the next
    message; `ProtocolError` if EOF arrives mid-message (a truncated
    header or payload)."""
    header = _read_exactly(f, _HEADER.size)
    if header is None:
        return None
    if len(header) < _HEADER.size:
        raise ProtocolError("truncated message header at EOF")
    kind, length = _HEADER.unpack(header)
    payload = _read_exactly(f, length)
    if payload is None or len(payload) < length:
        raise ProtocolError("truncated message payload at EOF")
    return kind, payload


def _read_exactly(f: BinaryIO, n: int) -> bytes | None:
    """Reads exactly `n` bytes, or as many as EOF allows. `None` only when
    EOF arrives before any byte is read."""
    chunks = []
    remaining = n
    while remaining:
        chunk = f.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if not chunks:
        return None if n else b""
    return b"".join(chunks)
