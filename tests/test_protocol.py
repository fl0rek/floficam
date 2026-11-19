"""Length-prefixed binary messages between the feeder and the filter
child. See docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"The filter child" > "Protocol", and the plan's Global Constraints for the
type bytes and struct formats pinned here.
"""
import io

import pytest

from floficam.filter.protocol import (
    FRAME,
    HOLD_EMPTY,
    HOLD_GRAB,
    RELEASE,
    RESET,
    STATUS,
    Parser,
    ProtocolError,
    decode_frame_in,
    decode_frame_out,
    decode_reset,
    encode_control,
    encode_frame_in,
    encode_frame_out,
    encode_reset,
    encode_status,
    read_message,
)

FRAME_BYTES = b"\x01" * 32 + b"\x02" * 16  # a tiny stand-in frame


def test_roundtrip_each_message():
    p = Parser()

    msgs = p.feed(encode_frame_in(3, 123456789, FRAME_BYTES))
    assert len(msgs) == 1
    t, payload = msgs[0]
    assert t == FRAME
    assert decode_frame_in(payload) == (3, 123456789, FRAME_BYTES)

    msgs = p.feed(encode_frame_out(3, 123456789, True, FRAME_BYTES))
    t, payload = msgs[0]
    assert t == FRAME
    assert decode_frame_out(payload) == (3, 123456789, True, FRAME_BYTES)

    msgs = p.feed(encode_frame_out(3, 123456789, False, FRAME_BYTES))
    assert decode_frame_out(msgs[0][1]) == (3, 123456789, False, FRAME_BYTES)

    for kind in (HOLD_GRAB, HOLD_EMPTY, RELEASE):
        msgs = p.feed(encode_control(kind))
        assert msgs == [(kind, b"")]

    msgs = p.feed(encode_reset(7))
    assert msgs == [(RESET, b"\x07\x00\x00\x00")]
    assert decode_reset(msgs[0][1]) == 7

    d = {"preset": "vhs", "modified": True, "ok": True, "holding": "none", "ms_per_frame": 1.5}
    msgs = p.feed(encode_status(d))
    t, payload = msgs[0]
    assert t == STATUS
    import json

    assert json.loads(payload.decode("utf-8")) == d


def test_parser_splits_two_controls_in_one_chunk():
    assert [t for t, _ in Parser().feed(encode_reset(3) + encode_control(HOLD_EMPTY))] == [
        RESET,
        HOLD_EMPTY,
    ]


def test_parser_partial_frame_waits():
    m = encode_frame_in(1, 2, FRAME_BYTES)
    p = Parser()
    assert p.feed(m[:5]) == []
    rest = p.feed(m[5:])
    assert len(rest) == 1
    assert rest[0][0] == FRAME


def test_parser_partial_header_waits():
    m = encode_reset(9)
    p = Parser()
    # Header is 5 bytes ("<BI"); feed less than that.
    assert p.feed(m[:2]) == []
    assert p.feed(m[2:]) == [(RESET, b"\x09\x00\x00\x00")]


def test_read_message_eof_none_and_truncated_raises():
    assert read_message(io.BytesIO(b"")) is None

    # A complete message followed by a clean EOF reads fine.
    f = io.BytesIO(encode_control(RELEASE))
    assert read_message(f) == (RELEASE, b"")
    assert read_message(f) is None

    # Header present, payload truncated at EOF.
    msg = encode_reset(5)
    truncated = io.BytesIO(msg[:-1])
    with pytest.raises(ProtocolError):
        read_message(truncated)

    # Header itself truncated at EOF.
    truncated_header = io.BytesIO(msg[:2])
    with pytest.raises(ProtocolError):
        read_message(truncated_header)
