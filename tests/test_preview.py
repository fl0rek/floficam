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
import threading
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
        lines = _read_lines(proc, 6)
        assert lines[0] == "source scene"  # no --camera: the scene from the start
        lines = lines[1:]
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
        # No still lingers once the sheet is closed (a camera frame, say).
        assert list(out_dir.glob("frame-*.png")) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_preview_sweeps_stale_frames_at_start(tmp_path):
    """Frames a killed helper left in --out are gone before the new one
    says anything: never shown, never kept."""
    settings_path = tmp_path / "settings.json"
    _write_settings(settings_path, {"version": 1, "preset": "does-not-exist"})  # child silent: no new frames
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for name in ("frame-7.png", "frame-123.png"):
        (out_dir / name).write_bytes(b"stale")
    (out_dir / "keep.txt").write_text("not a frame")

    proc = _spawn(tmp_path, settings_path, out_dir)
    try:
        assert _read_lines(proc, 1) == ["source scene"]
        assert list(out_dir.glob("frame-*.png")) == []
        assert (out_dir / "keep.txt").exists()
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
        assert _read_lines(proc, 1) == ["source scene"]
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


# --- --camera: the real camera as the frame source --------------------------
#
# In process, through main()'s seams: `make_camera` builds a fake source
# (never a real camera or PipeWire), `stdin_fd` is a pipe the test closes
# for the sheet's stdin EOF, and `camera_timeout_s` stands in for the
# 5 s no-frame fallback. The filter child is still the real one.


class _FakeCamera:
    """`CameraSource`'s shape: start()/stop(), and frames or an error
    pushed from its own thread, as GStreamer's streaming thread would.
    `mode`: "frames" (a grey frame every 1/30 s), "silent" (nothing ever),
    "error" (one on_error shortly after start) or "stall" (a few frames,
    then nothing)."""

    def __init__(self, on_frame, on_error, mode):
        self._on_frame = on_frame
        self._on_error = on_error
        self._mode = mode
        self._halt = threading.Event()
        self._thread = None
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        if self._mode != "silent":
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        if self._mode == "error":
            if not self._halt.wait(0.05):
                self._on_error("fake pipewiresrc: target not found")
            return
        frame = b"\x80" * (preview.OUT_W * preview.OUT_H * 3 // 2)
        sent = 0
        while not self._halt.wait(1 / 30):
            if self._mode == "stall" and sent >= 10:
                continue
            self._on_frame(frame, time.monotonic())
            sent += 1

    def stop(self):
        self.stopped = True
        self._halt.set()
        if self._thread is not None:
            self._thread.join()


class _InProcess:
    """main() on a thread, with --camera and a fake camera. `out()`/
    `err()` accumulate what it printed (capsys sees every thread's
    writes); `eof()` closes its stdin and returns main()'s result."""

    def __init__(self, tmp_path, monkeypatch, capsys, mode, timeout_s=0.3):
        state_home = tmp_path / "state"
        state_home.mkdir(exist_ok=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        monkeypatch.setenv("FFSB_CAMERA_FILTER", f"{sys.executable} -m ffsb_camera.filter.child")
        monkeypatch.setenv("FFSB_CAMERA_SETTINGS", "")  # main() sets it; restored after
        settings_path = tmp_path / "settings.json"
        _write_settings(settings_path, {"version": 1, "preset": "none"})
        self._capsys = capsys
        self._out = ""
        self._err = ""
        self.cameras = []

        def make_camera(on_frame, on_error):
            cam = _FakeCamera(on_frame, on_error, mode)
            self.cameras.append(cam)
            return cam

        self._rfd, self._wfd = os.pipe()
        self._result = []
        argv = ["--settings", str(settings_path), "--out", str(tmp_path / "out"), "--fps", "20", "--camera"]
        self._thread = threading.Thread(
            target=lambda: self._result.append(preview.main(
                argv, stdin_fd=self._rfd, make_camera=make_camera, camera_timeout_s=timeout_s)),
            daemon=True,
        )
        self._thread.start()

    def _drain(self):
        captured = self._capsys.readouterr()
        self._out += captured.out
        self._err += captured.err

    def out_lines(self, n_frames, timeout=10.0) -> list[str]:
        """Every stdout line once `n_frames` frame lines are in."""
        deadline = time.monotonic() + timeout
        while True:
            self._drain()
            lines = self._out.splitlines()
            if sum(1 for line in lines if _LINE_RE.match(line)) >= n_frames:
                return lines
            if not self._thread.is_alive():
                raise AssertionError(f"main() returned {self._result!r} early; got {lines!r}")
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {n_frames} frame lines; got {lines!r}")
            time.sleep(0.05)

    def err(self) -> str:
        self._drain()
        return self._err

    def eof(self) -> int:
        os.close(self._wfd)
        self._thread.join(timeout=10.0)
        os.close(self._rfd)
        assert not self._thread.is_alive(), "main() still running after stdin EOF"
        return self._result[0]


def test_camera_frames_flow_and_source_camera_comes_first(tmp_path, monkeypatch, capsys):
    """(a) The fake camera's frames reach the child: `source camera`
    before (or with) the first frame line, no scene loaded at all."""
    loads = []
    monkeypatch.setattr(preview, "_load_scene", lambda path: loads.append(path) or b"")
    run = _InProcess(tmp_path, monkeypatch, capsys, "frames")
    try:
        lines = run.out_lines(3)
    finally:
        rc = run.eof()
    assert lines[0] == "source camera", lines
    assert "source scene" not in lines
    assert all(_LINE_RE.match(line) for line in lines[1:]), lines
    assert loads == []
    assert "camera unavailable" not in run.err()
    assert rc == 0


def test_camera_silent_falls_back_to_the_scene(tmp_path, monkeypatch, capsys):
    """(b) No camera frame within the timeout: one stderr line, the
    camera stopped, `source scene`, then scene frames."""
    run = _InProcess(tmp_path, monkeypatch, capsys, "silent", timeout_s=0.3)
    try:
        lines = run.out_lines(3)
        cam = run.cameras[0]
        assert cam.stopped, "the camera was not released on fallback"
    finally:
        rc = run.eof()
    assert lines[0] == "source scene", lines
    assert "source camera" not in lines
    assert all(_LINE_RE.match(line) for line in lines[1:]), lines
    err = run.err()
    assert ("ffsb-camera-filter-preview: camera unavailable: no frame within 0.3 s, using test scene\n"
            in err), err
    assert err.count("camera unavailable") == 1
    assert len(run.cameras) == 1
    assert rc == 0


def test_camera_error_falls_back_to_the_scene(tmp_path, monkeypatch, capsys):
    """(c) A source error (a bus ERROR, say): the same fallback, with
    the source's reason in the stderr line -- well before the timeout."""
    run = _InProcess(tmp_path, monkeypatch, capsys, "error", timeout_s=30.0)
    try:
        lines = run.out_lines(3)
        assert run.cameras[0].stopped
    finally:
        rc = run.eof()
    assert lines[0] == "source scene", lines
    err = run.err()
    assert ("ffsb-camera-filter-preview: camera unavailable: fake pipewiresrc: target not found, "
            "using test scene\n" in err), err
    assert rc == 0


def test_camera_stall_mid_stream_falls_back_to_the_scene(tmp_path, monkeypatch, capsys):
    """A live camera that stops sending frames for the timeout is a
    failure like the start-up one: the stderr line, the camera stopped,
    `source scene`, scene frames."""
    run = _InProcess(tmp_path, monkeypatch, capsys, "stall", timeout_s=0.3)
    try:
        lines = run.out_lines(1)
        deadline = time.monotonic() + 10.0
        while "source scene" not in lines and time.monotonic() < deadline:
            time.sleep(0.05)
            lines = run.out_lines(1)
        i = lines.index("source scene")
        n_before = sum(1 for line in lines[:i] if _LINE_RE.match(line))
        lines = run.out_lines(n_before + 3)
        assert run.cameras[0].stopped
    finally:
        rc = run.eof()
    assert lines[0] == "source camera", lines
    assert lines.count("source scene") == 1, lines
    err = run.err()
    assert ("ffsb-camera-filter-preview: camera unavailable: no frame for 0.3 s, using test scene\n"
            in err), err
    assert err.count("camera unavailable") == 1
    assert rc == 0


def test_stdin_eof_stops_the_camera(tmp_path, monkeypatch, capsys):
    """(d) The sheet closing (stdin EOF) releases the camera, and main()
    still returns 0."""
    run = _InProcess(tmp_path, monkeypatch, capsys, "frames")
    try:
        run.out_lines(1)
        cam = run.cameras[0]
        assert cam.started and not cam.stopped
    finally:
        rc = run.eof()
    assert cam.stopped
    assert rc == 0


# --- CameraSource against real GStreamer, no PipeWire -----------------------
#
# Its threading and bus handling, through the `make_pipeline` seam: a
# live videotestsrc named `src` stands in for pipewiresrc.

_TEST_PIPELINE = (
    "videotestsrc name=src is-live=true {extra} ! "
    f"video/x-raw,format=NV12,width={preview.OUT_W},height={preview.OUT_H},framerate=30/1 ! "
    "appsink name=sink max-buffers=1 drop=true sync=false"
)


def _gst_pipeline(extra=""):
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    return lambda: Gst.parse_launch(_TEST_PIPELINE.format(extra=extra))


def test_camera_source_delivers_frames_and_stops_to_null():
    from gi.repository import Gst

    frames, errors = [], []
    made = []

    def make():
        p = _gst_pipeline()()
        made.append(p)
        return p

    cam = preview.CameraSource(lambda f, t: frames.append((len(f), t)), errors.append, make_pipeline=make)
    cam.start()
    deadline = time.monotonic() + 5.0
    while len(frames) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    cam.stop()
    assert len(frames) >= 3, frames
    assert frames[0][0] == preview.OUT_W * preview.OUT_H * 3 // 2
    assert errors == []
    _, state, _ = made[0].get_state(0)
    assert state == Gst.State.NULL
    n = len(frames)
    time.sleep(0.2)
    assert len(frames) == n, "frames delivered after stop()"


def test_camera_source_reports_a_loop_exception_as_an_error():
    """Anything raising on the camera thread (here: a pipeline with no
    `sink` to pull from) reaches on_error, so the helper falls back
    instead of waiting out the timeout on a dead thread."""
    from gi.repository import Gst

    def make():
        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.parse_launch("videotestsrc name=src is-live=true ! fakesink")

    errors = []
    cam = preview.CameraSource(lambda f, t: None, errors.append, make_pipeline=make)
    cam.start()
    deadline = time.monotonic() + 5.0
    while not errors and time.monotonic() < deadline:
        time.sleep(0.02)
    cam.stop()
    assert len(errors) == 1, errors


def test_camera_source_reports_eos_as_an_error():
    """A source that ends (EOS) is a failure for a camera, not a
    clean finish: on_error fires once."""
    frames, errors = [], []
    cam = preview.CameraSource(lambda f, t: frames.append(t), errors.append,
                               make_pipeline=_gst_pipeline("num-buffers=2"))
    cam.start()
    deadline = time.monotonic() + 5.0
    while not errors and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)
    cam.stop()
    assert len(errors) == 1, errors
    assert "EOS" in errors[0]


def test_camera_pipeline_is_an_ordinary_autoconnected_stream():
    """The capture script treats `firefox-sandbox.lane=camera-feed` as
    the sandbox feeder's input: the preview's stream must never carry a
    `firefox-sandbox` key, and must let WirePlumber's policy link it. Its
    application.name is what the tray lists while it holds the camera.
    Parsed at NULL: no PipeWire connection is made."""
    from gi.repository import Gst

    assert "firefox-sandbox" not in preview.CAMERA_PIPELINE
    if not Gst.is_initialized():
        Gst.init(None)
    src = Gst.parse_launch(preview.CAMERA_PIPELINE).get_by_name("src")
    assert src.get_factory().get_name() == "pipewiresrc"
    assert src.props.autoconnect is True
    assert src.props.client_name == "ffsb-camera-preview"
    props = src.props.stream_properties
    assert props.get_string("application.name") == "Camera Properties preview"
    assert not any(props.nth_field_name(i).startswith("firefox-sandbox") for i in range(props.n_fields()))
