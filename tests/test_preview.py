"""`ffsb-camera-filter-preview`: the Control Panel applet's preview
helper. Runs the real entry points as subprocesses (the filter child via
`FFSB_CAMERA_FILTER`, same override `locate.child_argv` understands) so
the tests exercise the real pipe protocol, not a mock. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"Preview".
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from ffsb_camera.filter import preview
from ffsb_camera.filter.png import encode_rgb

_LINE_RE = re.compile(r"^frame-(\d+)\.png (\S+)$")


def test_png_structure():
    import struct
    import zlib

    w, h = 1280, 720
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :, 0] = 200  # some non-zero content

    data = encode_rgb(rgb)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

    pos = 8
    chunks = []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        crc = data[pos + 8 + length : pos + 12 + length]
        assert struct.unpack(">I", crc)[0] == zlib.crc32(kind + payload) & 0xFFFFFFFF
        chunks.append((kind, payload))
        pos += 12 + length

    kinds = [k for k, _ in chunks]
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]

    ihdr = chunks[0][1]
    width, height, bit_depth, colour_type, *_ = struct.unpack(">IIBBBBB", ihdr)
    assert (width, height, bit_depth, colour_type) == (1280, 720, 8, 2)

    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == h * (1 + w * 3)


def _child_env(tmp_path: Path, extra: dict | None = None) -> dict:
    """The env a preview subprocess needs to run for real: a HOME/
    XDG_STATE_HOME the filter child's state_dir() can resolve, the
    GStreamer/GI search paths the dev loop and the nix build both set up,
    and FFSB_CAMERA_FILTER pointed at the real child module (same
    override `locate.child_argv` understands -- no sibling binary exists
    in a pytest run)."""
    state_home = tmp_path / "state"
    state_home.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "XDG_STATE_HOME": str(state_home),
        "HOME": str(home),
        "FFSB_CAMERA_FILTER": f"{sys.executable} -m ffsb_camera.filter.child",
    }
    for key, value in os.environ.items():
        if key.startswith("FFSB_"):
            continue
        if key in ("PYTHONPATH", "GI_TYPELIB_PATH", "GST_PLUGIN_SYSTEM_PATH_1_0", "LD_LIBRARY_PATH"):
            env[key] = value
    if extra:
        env.update(extra)
    return env


def _write_settings(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc))


def _read_lines(proc, n, timeout=10.0):
    buf = b""
    lines = []
    deadline = time.monotonic() + timeout
    while len(lines) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {n} lines; got {lines!r}; buf={buf!r}")
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise AssertionError(f"stdout closed early; got {lines!r}; buf={buf!r}")
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            lines.append(line.decode())
    return lines


def _no_more_output(proc, wait_s=0.5) -> bool:
    ready, _, _ = select.select([proc.stdout], [], [], wait_s)
    return not ready


def _spawn(tmp_path, settings_path, out_dir, *, fps="20", env_extra=None, extra_args=()):
    out_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ffsb_camera.filter.preview",
            "--settings",
            str(settings_path),
            "--out",
            str(out_dir),
            "--fps",
            fps,
            *extra_args,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(tmp_path, env_extra),
    )


def test_preview_writes_rotating_frames(tmp_path):
    settings_path = tmp_path / "settings.json"
    _write_settings(settings_path, {"version": 1, "preset": "none"})
    out_dir = tmp_path / "out"

    proc = _spawn(tmp_path, settings_path, out_dir)
    try:
        lines = _read_lines(proc, 5)
        for line in lines:
            m = _LINE_RE.match(line)
            assert m, line
            float(m.group(2))  # parses as a number
        names = [int(_LINE_RE.match(line).group(1)) for line in lines]
        assert names == sorted(names)
        assert len(set(names)) == len(names)

        pngs = sorted(out_dir.glob("frame-*.png"))
        assert 1 <= len(pngs) <= 3

        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_preview_settings_error_prints_nothing_but_stays_up(tmp_path):
    settings_path = tmp_path / "settings.json"
    _write_settings(settings_path, {"version": 1, "preset": "does-not-exist"})
    out_dir = tmp_path / "out"

    proc = _spawn(tmp_path, settings_path, out_dir)
    try:
        assert _no_more_output(proc, wait_s=1.5)
        assert proc.poll() is None, "helper exited despite a settings error"
        assert list(out_dir.glob("frame-*.png")) == []

        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _make_test_png(path: Path) -> None:
    """A tiny, real PNG -- built with the same encoder under test, but
    that only means it's a valid 64x64 RGB8 PNG gst's pngdec can decode;
    it says nothing about preview.py's own correctness."""
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[:, :, 1] = 180
    path.write_bytes(encode_rgb(rgb))


def test_preview_missing_image_falls_back_to_bars(tmp_path):
    """A --image that doesn't exist must not hang the helper (filesrc
    fails the state change synchronously): bars, logged, frames still
    flow."""
    settings_path = tmp_path / "settings.json"
    _write_settings(settings_path, {"version": 1, "preset": "none"})
    out_dir = tmp_path / "out"
    missing = tmp_path / "does-not-exist.png"

    proc = _spawn(tmp_path, settings_path, out_dir, extra_args=["--image", str(missing)])
    try:
        lines = _read_lines(proc, 3, timeout=10.0)
        assert lines
        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0
        stderr = proc.stderr.read().decode()
        assert f"cannot load {missing}" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_preview_corrupt_image_falls_back_to_bars(tmp_path):
    """A --image that exists but isn't a real image (decodebin's
    typefind fails, async, on the bus): bounded by _load_image's bus
    poll, not by the 5 s overall timeout -- bars, logged, frames still
    flow."""
    settings_path = tmp_path / "settings.json"
    _write_settings(settings_path, {"version": 1, "preset": "none"})
    out_dir = tmp_path / "out"
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(os.urandom(4096))

    proc = _spawn(tmp_path, settings_path, out_dir, extra_args=["--image", str(corrupt)])
    try:
        lines = _read_lines(proc, 3, timeout=10.0)
        assert lines
        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0
        stderr = proc.stderr.read().decode()
        assert f"cannot load {corrupt}" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_preview_image_loads(tmp_path):
    """A real, decodable --image is used as-is: no fallback log line."""
    settings_path = tmp_path / "settings.json"
    _write_settings(settings_path, {"version": 1, "preset": "none"})
    out_dir = tmp_path / "out"
    image = tmp_path / "image.png"
    _make_test_png(image)

    proc = _spawn(tmp_path, settings_path, out_dir, extra_args=["--image", str(image)])
    try:
        lines = _read_lines(proc, 3, timeout=10.0)
        assert lines
        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0
        stderr = proc.stderr.read().decode()
        assert "cannot load" not in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_on_output_is_marshal_only(tmp_path, monkeypatch):
    """FilterLink's reader thread calls on_output under its delivery
    lock (filterlink.py's module docstring): it must only marshal, never
    block on the NV12->RGB->PNG conversion or the file write. A slow
    encode must not slow the callback down."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    writer = preview._Writer(out_dir)
    try:
        def slow_encode(rgb):
            time.sleep(0.5)
            return encode_rgb(rgb)

        monkeypatch.setattr(preview, "encode_rgb", slow_encode)

        frame = b"\x10" * (preview.OUT_W * preview.OUT_H * 3 // 2)
        t0 = time.monotonic()
        writer.on_output(frame, t0, False)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, elapsed
    finally:
        writer.stop()
