"""The filter child's run loop: settings (re)load, epoch-gated frames,
hold controls, STATUS cadence and stdin EOF. `run` is driven in a thread
over `os.pipe()` pairs so the test can write protocol bytes and read
replies without a real subprocess (`test_entry_point_subprocess` below
covers the real one). See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
filter child", and the plan's Global Constraints.
"""
import json
import os
import select
import subprocess
import sys
import threading
import time

from ffsb_camera.filter.child import run
from ffsb_camera.filter.locate import child_argv
from ffsb_camera.filter.protocol import (
    HOLD_EMPTY,
    HOLD_GRAB,
    STATUS,
    Parser,
    decode_frame_out,
    encode_control,
    encode_frame_in,
    encode_reset,
)

FRAME_A = b"A" * 64
FRAME_B = b"B" * 64
FRAME_C = b"C" * 64
FRAME_D = b"D" * 64


def _builtin_path(tmp_path):
    """A minimal valid builtin presets.json: `none` plus `vhs`, enough to
    exercise preset switching without the real package data."""
    p = tmp_path / "builtin-presets.json"
    p.write_text(json.dumps({
        "version": 1,
        "presets": [
            {"id": "none", "name": "None", "knobs": {}},
            {"id": "vhs", "name": "VHS", "knobs": {"picture": {"width": 640}}},
        ],
    }))
    return p


class ChildHarness:
    """Runs `child.run` in a background thread over two `os.pipe()`
    pairs: `write_in` sends protocol bytes as the feeder would, `recv`
    pulls parsed (type, payload) messages out as they arrive."""

    def __init__(self, tmp_path, *, clock=time.monotonic):
        self.tmp_path = tmp_path
        self.settings_path = tmp_path / "filter.json"
        self.user_presets_path = tmp_path / "presets.json"
        self.builtin_path = _builtin_path(tmp_path)
        self._in_r, self._in_w = os.pipe()
        self._out_r, self._out_w = os.pipe()
        self._parser = Parser()
        self._messages: list[tuple[int, bytes]] = []
        self._cv = threading.Condition()

        def target():
            out = os.fdopen(self._out_w, "wb", buffering=0)
            try:
                self.result = run(
                    self._in_r,
                    out,
                    self.settings_path,
                    self.user_presets_path,
                    self.builtin_path,
                    clock=clock,
                )
            finally:
                out.close()

        def reader():
            f = os.fdopen(self._out_r, "rb", buffering=0)
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                with self._cv:
                    self._messages.extend(self._parser.feed(chunk))
                    self._cv.notify_all()

        self._thread = threading.Thread(target=target, daemon=True)
        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._thread.start()
        self._reader_thread.start()

    def send(self, data: bytes) -> None:
        os.write(self._in_w, data)

    def close_input(self) -> None:
        os.close(self._in_w)

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        with self._cv:
            while not predicate(self._messages):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting; messages so far: {self._messages}")
                self._cv.wait(remaining)
            return list(self._messages)

    def messages(self) -> list:
        with self._cv:
            return list(self._messages)

    def join(self, timeout: float = 5.0) -> int:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "child.run did not exit"
        self._reader_thread.join(timeout)
        return self.result


def _statuses(msgs):
    return [json.loads(p.decode("utf-8")) for t, p in msgs if t == STATUS]


def test_status_at_startup_without_frames(tmp_path):
    h = ChildHarness(tmp_path)
    msgs = h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    assert msgs[0][0] == STATUS
    d = json.loads(msgs[0][1].decode("utf-8"))
    assert d["ok"] is True
    assert d["preset"] == "none"
    h.close_input()
    assert h.join() == 0


def test_status_on_settings_change_without_frames(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_text(json.dumps({"version": 1, "preset": "vhs"}))

    def saw_vhs(msgs):
        return any(d.get("preset") == "vhs" for d in _statuses(msgs))

    msgs = h.wait_for(saw_vhs, timeout=2.5)
    assert _statuses(msgs)[-1]["ok"] is True
    h.close_input()
    h.join()


def test_two_controls_one_write_handled_promptly(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    before = len(h.messages())
    h.send(encode_reset(1) + encode_control(HOLD_EMPTY))

    def saw_empty(msgs):
        return any(d.get("holding") == "empty" for d in _statuses(msgs[before:]))

    msgs = h.wait_for(saw_empty, timeout=0.3)
    assert not any(t for t, _ in msgs[before:] if t != STATUS)
    h.close_input()
    h.join()


def test_frame_echoes_epoch_and_pts(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.send(encode_reset(7))
    h.send(encode_frame_in(7, 123, FRAME_A))

    def got_frame(msgs):
        return any(t == 1 for t, _ in msgs)

    msgs = h.wait_for(got_frame, timeout=1.0)
    frame_msgs = [p for t, p in msgs if t == 1]
    assert decode_frame_out(frame_msgs[-1]) == (7, 123, False, FRAME_A)
    h.close_input()
    h.join()


def test_old_epoch_frame_ignored(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.send(encode_reset(8))
    h.send(encode_frame_in(7, 1, FRAME_A))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages())
    h.close_input()
    h.join()


def test_settings_error_then_recovery(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_text(json.dumps({"version": 1, "preset": "gone"}))
    h.send(encode_reset(1))

    def errored(msgs):
        return any(d.get("ok") is False for d in _statuses(msgs))

    msgs = h.wait_for(errored, timeout=2.0)
    assert "unknown preset" in _statuses(msgs)[-1]["error"]

    before = len(h.messages())
    for i, frame in enumerate((FRAME_A, FRAME_B, FRAME_C)):
        h.send(encode_frame_in(1, i, frame))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages()[before:])

    h.settings_path.write_text(json.dumps({"version": 1, "preset": "none"}))

    def recovered(msgs):
        return any(d.get("ok") is True for d in _statuses(msgs[before:]))

    h.wait_for(recovered, timeout=2.0)
    h.send(encode_frame_in(1, 99, FRAME_D))

    def got_d(msgs):
        return any(t == 1 and decode_frame_out(p)[2:] == (False, FRAME_D) for t, p in msgs)

    h.wait_for(got_d, timeout=1.0)
    h.close_input()
    h.join()


def test_hold_survives_reload(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.send(encode_reset(1))
    h.send(encode_frame_in(1, 0, FRAME_A))
    h.send(encode_control(HOLD_GRAB))
    h.send(encode_frame_in(1, 1, FRAME_B))

    def held_b(msgs):
        frames = [p for t, p in msgs if t == 1]
        return frames and decode_frame_out(frames[-1]) == (1, 1, True, FRAME_B)

    h.wait_for(held_b, timeout=1.0)

    h.settings_path.write_text(json.dumps({"version": 1, "preset": "vhs"}))
    before = len(h.messages())
    h.send(encode_frame_in(1, 2, FRAME_C))

    def held_b_again(msgs):
        frames = [p for t, p in msgs if t == 1]
        return frames and decode_frame_out(frames[-1]) == (1, 2, True, FRAME_B)

    h.wait_for(held_b_again, timeout=1.0)

    h.settings_path.write_text("{")
    before = len(h.messages())

    def errored(msgs):
        return any(d.get("ok") is False for d in _statuses(msgs))

    h.wait_for(errored, timeout=2.0)

    h.settings_path.write_text(json.dumps({"version": 1, "preset": "none"}))
    before = len(h.messages())
    h.send(encode_frame_in(1, 3, FRAME_D))

    def held_b_final(msgs):
        frames = [p for t, p in msgs if t == 1]
        return frames and decode_frame_out(frames[-1]) == (1, 3, True, FRAME_B)

    h.wait_for(held_b_final, timeout=2.0)
    h.close_input()
    h.join()


def test_exits_on_stdin_eof(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.close_input()
    assert h.join() == 0


def test_entry_point_subprocess(tmp_path):
    state_home = tmp_path / "state"
    state_home.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "XDG_STATE_HOME": str(state_home),
        "HOME": str(home),
    }
    for key, value in os.environ.items():
        if key.startswith("FFSB_"):
            continue
        if key in ("PYTHONPATH", "GI_TYPELIB_PATH", "GST_PLUGIN_SYSTEM_PATH_1_0", "LD_LIBRARY_PATH"):
            env[key] = value
    proc = subprocess.Popen(
        [sys.executable, "-m", "ffsb_camera.filter.child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        parser = Parser()
        msgs = []
        deadline = time.monotonic() + 5.0
        while not msgs and time.monotonic() < deadline:
            # A plain BufferedReader.read(n) blocks until `n` bytes
            # arrive or EOF -- the wrong tool here, since one STATUS
            # message is far short of 4096 bytes and the child keeps the
            # pipe open. os.read does a single syscall's worth instead.
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            msgs.extend(parser.feed(chunk))
        assert msgs, "no STATUS from the built entry point"
        assert msgs[0][0] == STATUS
        d = json.loads(msgs[0][1].decode("utf-8"))
        assert d["ok"] is True
        proc.stdin.close()
        rc = proc.wait(timeout=5.0)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_child_argv_sibling(tmp_path):
    assert child_argv({}, str(tmp_path / "bin/.ffsb-camera-feeder-wrapped")) == [
        str(tmp_path / "bin/ffsb-camera-filter")
    ]


def test_child_argv_override():
    assert child_argv({"FFSB_CAMERA_FILTER": "python3 -m ffsb_camera.filter.child"}, "x") == [
        "python3",
        "-m",
        "ffsb_camera.filter.child",
    ]
