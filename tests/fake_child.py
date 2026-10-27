"""A stand-in filter child for test_filterlink.py, run as
`[sys.executable, fake_child.py]`. Speaks the real protocol, imports no
GStreamer. Behaviour comes from the environment:

- `FAKE_MODE`: `echo` (default) answers each FRAME with an output FRAME
  carrying the input's epoch and pts (never held), then a STATUS
  `{"ok": true, "echoed": n}`; it also sends that STATUS once at
  start-up. Unlike the real child it echoes every epoch, so the feeder
  side's own epoch filter is what the tests see. `hang` reads (and
  records) but never writes a byte.
- `FAKE_DIE_AFTER=N`: exit after N messages read (0: exit at once).
- `FAKE_RECORD=path`: append every message read, raw, to `path`,
  flushed per message.
- `FAKE_GATE=path`: read nothing until `path` exists.
- `FAKE_DELAY_MS=ms`: sleep before each echo.
- `FAKE_ANSWER_GATE=path`: before each echo, wait until `path` exists,
  then remove it: one touch, one answer.
"""
import json
import os
import struct
import sys
import time

_HEADER = struct.Struct("<BI")
_FRAME_IN_HDR = struct.Struct("<IQ")
FRAME, STATUS = 1, 6


def _send(out, kind, payload):
    out.write(_HEADER.pack(kind, len(payload)) + payload)
    out.flush()


def _status(out, echoed):
    _send(out, STATUS, json.dumps({"ok": True, "echoed": echoed}).encode())


def _read_exactly(f, n):
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def main():
    mode = os.environ.get("FAKE_MODE", "echo")
    die_after = int(os.environ.get("FAKE_DIE_AFTER", "-1"))
    record = os.environ.get("FAKE_RECORD")
    gate = os.environ.get("FAKE_GATE")
    delay = int(os.environ.get("FAKE_DELAY_MS", "0")) / 1000
    answer_gate = os.environ.get("FAKE_ANSWER_GATE")
    stdin, out = sys.stdin.buffer, sys.stdout.buffer
    rec = open(record, "ab", buffering=0) if record else None

    if die_after == 0:
        return 0
    if mode == "echo":
        _status(out, 0)
    while gate and not os.path.exists(gate):
        time.sleep(0.01)

    seen = echoed = 0
    while True:
        header = _read_exactly(stdin, _HEADER.size)
        if header is None:
            return 0
        kind, length = _HEADER.unpack(header)
        payload = _read_exactly(stdin, length) if length else b""
        if payload is None:
            return 0
        if rec:
            rec.write(header + payload)
        seen += 1
        if mode == "echo" and kind == FRAME:
            epoch, pts_ns = _FRAME_IN_HDR.unpack_from(payload)
            frame = payload[_FRAME_IN_HDR.size:]
            if answer_gate:
                while not os.path.exists(answer_gate):
                    time.sleep(0.01)
                os.remove(answer_gate)
            time.sleep(delay)
            _send(out, FRAME, struct.pack("<IQB", epoch, pts_ns, 0) + frame)
            echoed += 1
            _status(out, echoed)
        if seen == die_after:
            return 0


if __name__ == "__main__":
    sys.exit(main())
