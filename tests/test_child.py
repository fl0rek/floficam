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

import pytest

from floficam.filter import chain as chain_mod
from floficam.filter.child import FRAME_BYTES, run
from floficam.filter.locate import child_argv
from floficam.filter.protocol import (
    HOLD_EMPTY,
    HOLD_GRAB,
    STATUS,
    Parser,
    decode_frame_out,
    encode_control,
    encode_frame_in,
    encode_reset,
)

# Real 1280x720 NV12 lengths (the child drops anything else), flat
# content -- the presets below keep every pixel knob at zero, so outputs
# compare equal to these bytes.
FRAME_A = b"A" * FRAME_BYTES
FRAME_B = b"B" * FRAME_BYTES
FRAME_C = b"C" * FRAME_BYTES
FRAME_D = b"D" * FRAME_BYTES


def _builtin_path(tmp_path):
    """A minimal valid builtin presets.json: `none` plus `vhs`, enough to
    exercise preset switching without the real package data. This `vhs`
    only caps the frame rate: every pixel knob stays at its zero setting,
    where the chain passes bytes through untouched, so the tests can
    compare outputs to their input frames byte for byte."""
    p = tmp_path / "builtin-presets.json"
    p.write_text(json.dumps({
        "version": 1,
        "presets": [
            {"id": "none", "name": "None", "knobs": {}},
            {"id": "vhs", "name": "VHS", "knobs": {"temporal": {"fps_cap": 15}}},
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
        # A frame is far bigger than a pipe buffer: a blocking write to a
        # child that died would hang the test (and the nix check phase)
        # instead of failing it. `send` waits with a deadline instead.
        os.set_blocking(self._in_w, False)
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

    def send(self, data: bytes, timeout: float = 5.0) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out writing to the child (is it dead?)")
            _, ready, _ = select.select([], [self._in_w], [], remaining)
            if ready:
                try:
                    view = view[os.write(self._in_w, view):]
                except BlockingIOError:
                    pass

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


def test_hold_grab_status_reports_empty_until_captured(tmp_path):
    """R6: HOLD_GRAB alone (nothing captured yet) reports holding "empty";
    the FRAME that gets captured both answers with a held output and
    forces a STATUS reporting holding "frame", promptly, in that same
    batch."""
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.send(encode_reset(3))
    h.send(encode_frame_in(3, 0, FRAME_A))
    before = len(h.messages())
    h.send(encode_control(HOLD_GRAB))

    def saw_empty(msgs):
        return any(d.get("holding") == "empty" for d in _statuses(msgs[before:]))

    h.wait_for(saw_empty, timeout=1.0)

    before = len(h.messages())
    h.send(encode_frame_in(3, 1, FRAME_B))

    def captured(msgs):
        tail = msgs[before:]
        frames = [p for t, p in tail if t == 1]
        got_frame = frames and decode_frame_out(frames[-1]) == (3, 1, True, FRAME_B)
        got_status = any(d.get("holding") == "frame" for d in _statuses(tail))
        return got_frame and got_status

    h.wait_for(captured, timeout=1.0)
    h.close_input()
    h.join()


def test_hold_empty_after_reset_emits_nothing(tmp_path):
    """What FilterLink sends on reset() while a hold is remembered: the
    RESET releases the chain's hold, the HOLD_EMPTY right behind it
    re-takes it before any frame, so nothing comes out."""
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.send(encode_reset(1) + encode_control(HOLD_EMPTY) + encode_frame_in(1, 0, FRAME_A))

    def saw_empty(msgs):
        return any(d.get("holding") == "empty" for d in _statuses(msgs))

    h.wait_for(saw_empty, timeout=1.0)
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages())
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


def _errored(msgs):
    return any(d.get("ok") is False for d in _statuses(msgs))


def _recovers(h):
    """A valid `none` write brings frames back: the child survived."""
    before = len(h.messages())
    h.settings_path.write_text(json.dumps({"version": 1, "preset": "none"}))
    h.wait_for(lambda m: any(d.get("ok") is True for d in _statuses(m[before:])), timeout=2.5)
    h.send(encode_frame_in(1, 99, FRAME_D))
    h.wait_for(lambda m: any(t == 1 and decode_frame_out(p)[2:] == (False, FRAME_D)
                             for t, p in m[before:]), timeout=1.0)


def test_non_utf8_filter_json_fails_closed(tmp_path):
    """I3: an unreadable settings file is ok:false and no frames, never a
    crash (a crash-looping child reads as "filter not running")."""
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_bytes(b'{"version":1,"preset":"\xff"}')
    h.send(encode_reset(1))
    msgs = h.wait_for(_errored, timeout=2.5)
    assert "filter.json" in _statuses(msgs)[-1]["error"]
    h.send(encode_frame_in(1, 0, FRAME_A))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages())
    _recovers(h)
    h.close_input()
    assert h.join() == 0


def test_directory_at_filter_json_fails_closed(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.mkdir()
    h.send(encode_reset(1))
    h.wait_for(_errored, timeout=2.5)
    h.send(encode_frame_in(1, 0, FRAME_A))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages())
    h.settings_path.rmdir()
    _recovers(h)
    h.close_input()
    assert h.join() == 0


def test_list_valued_preset_fails_closed(tmp_path):
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_text(json.dumps({"version": 1, "preset": ["vhs"]}))
    h.send(encode_reset(1))
    h.wait_for(_errored, timeout=2.5)
    h.send(encode_frame_in(1, 0, FRAME_A))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages())
    _recovers(h)
    h.close_input()
    assert h.join() == 0


def test_unexpected_reload_failure_fails_closed(tmp_path, monkeypatch, capsys):
    """Whatever else goes wrong reading the settings is a settings error,
    logged with its text, not a crash loop."""
    from floficam.filter import child as child_mod

    def broken(*_):
        raise RuntimeError("boom")

    monkeypatch.setattr(child_mod, "load_presets", broken)
    h = ChildHarness(tmp_path)
    msgs = h.wait_for(_errored, timeout=2.0)
    assert "boom" in _statuses(msgs)[-1]["error"]
    h.send(encode_reset(1) + encode_frame_in(1, 0, FRAME_A))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages())
    h.close_input()
    assert h.join() == 0
    assert "boom" in capsys.readouterr().err


@pytest.mark.parametrize("active", [None, "none", "vhs"])
def test_non_utf8_user_presets_not_fatal(tmp_path, active):
    """Spec: presets.json is never fatal on its own. An unreadable one
    means no user presets; `none` and the built-ins keep running."""
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    if active is not None:
        h.settings_path.write_text(json.dumps({"version": 1, "preset": active}))
    h.user_presets_path.write_bytes(b'{"version":1,"presets":[{"id":"\xff"}]}')
    h.send(encode_reset(1))
    want = active or "none"

    def loaded(msgs):
        return any(d.get("preset") == want and d.get("ok") is True for d in _statuses(msgs))

    h.wait_for(loaded, timeout=2.5)
    h.send(encode_frame_in(1, 5, FRAME_A))
    h.wait_for(lambda m: any(t == 1 and decode_frame_out(p) == (1, 5, False, FRAME_A)
                             for t, p in m), timeout=1.0)
    assert not _errored(h.messages())
    h.close_input()
    assert h.join() == 0


def test_codec_build_failure_is_a_settings_error(tmp_path, monkeypatch):
    """R12: a codec stage that fails to build (a missing GStreamer
    plugin, say) fails closed and visibly: STATUS ok:false with the
    reason, no FRAME out, until a later valid settings write."""
    real = chain_mod.CodecStage

    def codec_stage(codec, fps_cap, w, h, rng):
        if codec.kind != "off":
            raise RuntimeError('no element "jpegenc"')
        return real(codec, fps_cap, w, h, rng)

    monkeypatch.setattr(chain_mod, "CodecStage", codec_stage)
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_text(json.dumps(
        {"version": 1, "preset": "none", "overrides": {"codec": {"kind": "jpeg"}}}
    ))
    h.send(encode_reset(1))

    def errored(msgs):
        return any(d.get("ok") is False for d in _statuses(msgs))

    msgs = h.wait_for(errored, timeout=2.0)
    assert "jpegenc" in _statuses(msgs)[-1]["error"]

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


def test_codec_rebuild_failure_on_reset(tmp_path, monkeypatch):
    """R12 through the child's RESET branch: the active knobs' codec
    built fine once, but its rebuild on RESET fails -- STATUS ok:false
    with the reason and no FRAME out, until a later valid settings
    write."""
    real = chain_mod.CodecStage
    builds = []

    def codec_stage(codec, fps_cap, w, h, rng):
        if codec.kind != "off":
            builds.append(codec)
            if len(builds) == 2:
                raise RuntimeError('no element "jpegdec"')
        return real(codec, fps_cap, w, h, rng)

    monkeypatch.setattr(chain_mod, "CodecStage", codec_stage)
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_text(json.dumps(
        {"version": 1, "preset": "none", "overrides": {"codec": {"kind": "jpeg"}}}
    ))

    def jpeg_loaded(msgs):
        return any(d.get("modified") is True and d.get("ok") is True for d in _statuses(msgs))

    h.wait_for(jpeg_loaded, timeout=2.5)
    assert len(builds) == 1

    before = len(h.messages())
    h.send(encode_reset(1))

    def errored(msgs):
        return any(d.get("ok") is False for d in _statuses(msgs[before:]))

    msgs = h.wait_for(errored, timeout=1.0)
    assert len(builds) == 2
    assert "jpegdec" in [d for d in _statuses(msgs[before:]) if d["ok"] is False][0]["error"]

    before = len(h.messages())
    for i, frame in enumerate((FRAME_A, FRAME_B)):
        h.send(encode_frame_in(1, i, frame))
    time.sleep(0.3)
    assert not any(t == 1 for t, _ in h.messages()[before:])

    h.settings_path.write_text(json.dumps({"version": 1, "preset": "none"}))

    def recovered(msgs):
        return any(d.get("ok") is True for d in _statuses(msgs[before:]))

    h.wait_for(recovered, timeout=2.5)
    h.send(encode_frame_in(1, 99, FRAME_D))

    def got_d(msgs):
        return any(t == 1 and decode_frame_out(p)[2:] == (False, FRAME_D) for t, p in msgs)

    h.wait_for(got_d, timeout=1.0)
    h.close_input()
    h.join()


def test_wrong_size_frame_is_dropped_not_fatal(tmp_path, capsys):
    """R14: an input of the wrong length never reaches the chain (whose
    pixel stages would raise on it): no output, one log line per distinct
    length, and the next good frame goes through."""
    h = ChildHarness(tmp_path)
    h.wait_for(lambda m: len(m) >= 1, timeout=2.0)
    h.settings_path.write_text(json.dumps(
        {"version": 1, "preset": "none", "overrides": {"picture": {"width": 320, "tint": "mono"}}}
    ))

    def loaded(msgs):
        return any(d.get("modified") is True and d.get("ok") is True for d in _statuses(msgs))

    h.wait_for(loaded, timeout=2.5)
    h.send(encode_reset(1))
    h.send(encode_frame_in(1, 0, FRAME_A[:-640]))
    h.send(encode_frame_in(1, 1, FRAME_A[:-640]))
    h.send(encode_frame_in(1, 2, FRAME_B))

    def got_frame(msgs):
        return any(t == 1 for t, _ in msgs)

    msgs = h.wait_for(got_frame, timeout=2.0)
    outs = [decode_frame_out(p) for t, p in msgs if t == 1]
    assert [(e, pts, held) for e, pts, held, _ in outs] == [(1, 2, False)]
    assert len(outs[0][3]) == FRAME_BYTES and outs[0][3] != FRAME_B
    h.close_input()
    assert h.join() == 0
    log = capsys.readouterr().err
    want = f"floficam-filter: dropping input frames of {FRAME_BYTES - 640} bytes, want 1382400"
    assert log.count(want) == 1


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
    h.send(encode_frame_in(1, 2, FRAME_C))

    def held_b_again(msgs):
        frames = [p for t, p in msgs if t == 1]
        return frames and decode_frame_out(frames[-1]) == (1, 2, True, FRAME_B)

    h.wait_for(held_b_again, timeout=1.0)

    h.settings_path.write_text("{")

    def errored(msgs):
        return any(d.get("ok") is False for d in _statuses(msgs))

    h.wait_for(errored, timeout=2.0)

    h.settings_path.write_text(json.dumps({"version": 1, "preset": "none"}))
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
        if key.startswith("FLOFICAM_"):
            continue
        if key in ("PYTHONPATH", "GI_TYPELIB_PATH", "GST_PLUGIN_SYSTEM_PATH_1_0", "LD_LIBRARY_PATH"):
            env[key] = value
    proc = subprocess.Popen(
        [sys.executable, "-m", "floficam.filter.child"],
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
    assert child_argv({}, str(tmp_path / "bin/.floficam-daemon-wrapped")) == [
        str(tmp_path / "bin/floficam-filter")
    ]


def test_child_argv_override():
    assert child_argv({"FLOFICAM_FILTER": "python3 -m floficam.filter.child"}, "x") == [
        "python3",
        "-m",
        "floficam.filter.child",
    ]
