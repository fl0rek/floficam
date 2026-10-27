"""`ffsb-camera-filter-preview`: the Control Panel applet's preview
helper. Spawns the same `ffsb-camera-filter` binary the feeder uses,
pointed at a settings file the applet rewrites on every pending edit,
and feeds it a canned, slowly panning test scene at 10 fps -- never the
camera, so it works while the lane is locked. Each filtered output is
written as a PNG the applet's `Image` element can show. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"Preview".

The scene is loaded once, at 1408x792 (128px wider and 72px taller than
the 1280x720 the chain expects), and a 1280x720 window into it pans 2px
per frame horizontally, bouncing at the edges -- enough motion for
temporal effects (shutter ghosting, interlace combing, codec mosh)
without a second real video file, and a vertical offset fixed at the
centre since the width margin alone gives 65 distinct offsets. The step
stays even so the UV crop's halved offset is always exact, never a
floor-rounded half-chroma-sample wobble.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from ..filterlink import FilterLink
from ..frames import render_bars
from .locate import child_argv
from .settings import preview_dir
from .stages import from_nv12, nv12_to_rgb, to_nv12
from .png import encode_rgb

OUT_W, OUT_H = 1280, 720
SCENE_W, SCENE_H = OUT_W + 128, OUT_H + 72
_V_OFFSET = (SCENE_H - OUT_H) // 2  # 36: fixed, centred
_PAN_MAX = SCENE_W - OUT_W  # 128: the horizontal bounce's far edge
_PAN_STEP = 2  # even, so h_offset // 2 (the UV crop) is always exact

_KEEP = 3  # frames kept on disk at once
_EPOCH = 1  # this helper never resets or respawns input: one epoch for its life

# _load_image's whole bounded wait for a frame (a missing or corrupt
# --image must never hang the helper), and the slice of it spent in each
# try-pull-preroll call -- short, so a bus ERROR (a corrupt file's
# typefind failure, say) is noticed promptly rather than waited out.
_LOAD_TIMEOUT_S = 5.0
_LOAD_POLL_S = 0.1


def _log(msg: str) -> None:
    print(f"ffsb-camera-filter-preview: {msg}", file=sys.stderr, flush=True)


def _bus_failure_text(bus) -> str | None:
    """A pending ERROR, or an EOS reached before any sample arrived --
    both mean the load failed. The EOS case matters on its own: a
    decoder fed bad input can swallow it silently rather than erroring
    (pngdec on random bytes typefound as PNG by `--image`'s extension,
    say), so the pipeline just runs dry instead of ever posting an
    ERROR. Non-blocking either way: `pop_filtered` returns at once."""
    msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
    if msg is None:
        return None
    if msg.type == Gst.MessageType.EOS:
        return "reached EOS before producing a frame"
    err, debug = msg.parse_error()
    return f"{err}: {debug}" if debug else str(err)


def _load_image(path: str) -> bytes:
    """One frame of `filesrc ! decodebin ! videoconvert ! videoscale`,
    scaled to the scene size -- the same pattern frames.render_bars uses
    for videotestsrc. Bounded to `_LOAD_TIMEOUT_S` total: a missing file
    fails the state change synchronously (`ret == FAILURE`, below, with
    the reason already on the bus); a corrupt one fails later and async
    (decodebin's typefind), so this polls short try-pull-preroll waits
    against the bus in between, rather than one pull-preroll that would
    block until GStreamer itself gives up -- which, for some failures,
    is never."""
    if not Gst.is_initialized():
        Gst.init(None)

    pipeline = Gst.parse_launch(
        f'filesrc location="{path}" ! decodebin ! videoconvert ! videoscale ! '
        f"video/x-raw,format=NV12,width={SCENE_W},height={SCENE_H} ! "
        "appsink name=sink sync=false max-buffers=1"
    )
    sink = pipeline.get_by_name("sink")
    bus = pipeline.get_bus()
    try:
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(_bus_failure_text(bus) or "state change to PLAYING failed")

        deadline = time.monotonic() + _LOAD_TIMEOUT_S
        sample = None
        while time.monotonic() < deadline:
            sample = sink.emit("try-pull-preroll", int(_LOAD_POLL_S * Gst.SECOND))
            if sample is not None:
                break
            failure_text = _bus_failure_text(bus)
            if failure_text is not None:
                raise RuntimeError(failure_text)
        if sample is None:
            raise RuntimeError(f"no frame within {_LOAD_TIMEOUT_S:g} s")

        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("could not map the decoded buffer")
        try:
            return bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
    finally:
        pipeline.set_state(Gst.State.NULL)


def _load_scene(image_path: str | None) -> bytes:
    """Falls back to the SMPTE bars on any load failure -- a bad
    `--image` must degrade the preview, never take the helper down."""
    if image_path:
        try:
            return _load_image(image_path)
        except Exception as e:
            _log(f"cannot load {image_path}: {e}, using bars")
    return render_bars(SCENE_W, SCENE_H)


def _bounce(offset: int, direction: int) -> tuple[int, int]:
    offset += direction * _PAN_STEP
    if offset <= 0:
        return 0, 1
    if offset >= _PAN_MAX:
        return _PAN_MAX, -1
    return offset, direction


class _Scene:
    """The loaded test scene, held as planes once so each tick only
    slices and re-packs its 1280x720 window."""

    def __init__(self, buf: bytes) -> None:
        self._y, self._uv = from_nv12(buf, SCENE_W, SCENE_H)

    def crop(self, h_offset: int) -> bytes:
        y = self._y[_V_OFFSET : _V_OFFSET + OUT_H, h_offset : h_offset + OUT_W]
        uv_h = h_offset // 2
        uv = self._uv[_V_OFFSET // 2 : _V_OFFSET // 2 + OUT_H // 2, uv_h : uv_h + OUT_W // 2]
        return to_nv12((y, uv))


class _Writer:
    """Turns the child's output frames into the files and stdout lines
    the spec pins: `.tmp` then a rename to `frame-<n>.png`, a printed
    `frame-<n>.png <ms>` line, and only the last `_KEEP` files kept. `ms`
    is the latest STATUS `ms_per_frame`, so a status that never arrives
    (impossible: the child sends one at start-up) would print 0.0.

    `on_output`/`on_status` run on FilterLink's reader thread, under its
    delivery lock (see filterlink.py's module docstring): they must
    marshal only, never block on the NV12->RGB->PNG conversion or file
    I/O below. `on_output` just replaces a one-frame pending slot
    (newest wins -- a frame the writer thread hasn't gotten to yet is
    dropped, never queued) and wakes the writer thread, which does the
    actual work."""

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir
        self._tmp = out_dir / ".tmp"
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._ms = 0.0
        self._pending: bytes | None = None
        self._stopped = False
        self._n = 0
        self._kept: list[int] = []
        self._thread = threading.Thread(target=self._run, name="ffsb-preview-writer", daemon=True)
        self._thread.start()

    def on_status(self, status: dict) -> None:
        with self._lock:
            self._ms = status.get("ms_per_frame", self._ms)

    def on_output(self, frame: bytes, input_t: float, held: bool) -> None:
        with self._cv:
            self._pending = frame
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify_all()
        self._thread.join()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._stopped:
                    self._cv.wait()
                if self._pending is None:
                    return  # stopped, and nothing left to write
                frame, self._pending = self._pending, None
                ms = self._ms
            self._emit(frame, ms)

    def _emit(self, frame: bytes, ms: float) -> None:
        rgb = nv12_to_rgb(from_nv12(frame, OUT_W, OUT_H))
        png_bytes = encode_rgb(rgb)
        with self._lock:
            self._n += 1
            n = self._n
            self._tmp.write_bytes(png_bytes)
            final = self._out_dir / f"frame-{n}.png"
            self._tmp.rename(final)
            self._kept.append(n)
            stale = self._kept[: -_KEEP] if len(self._kept) > _KEEP else []
            self._kept = self._kept[-_KEEP:]
        print(f"frame-{n}.png {ms}", flush=True)
        for old in stale:
            try:
                (self._out_dir / f"frame-{old}.png").unlink()
            except FileNotFoundError:
                pass


def _watch_stdin(stop: threading.Event) -> None:
    """Sets `stop` on stdin EOF (the sheet closed). Read on its own
    thread so the 10 fps submit loop below is never blocked on it."""
    try:
        while True:
            if not os.read(0, 4096):
                break
    except OSError:
        pass
    stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ffsb-camera-filter-preview", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--settings", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--image")
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else preview_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    # The child reads its settings from its own environment: inherited by
    # every subprocess FilterLink spawns below, same process for this
    # helper's whole life.
    os.environ["FFSB_CAMERA_SETTINGS"] = str(Path(args.settings))

    scene = _Scene(_load_scene(args.image))
    writer = _Writer(out_dir)

    link = FilterLink(
        child_argv(),
        on_output=writer.on_output,
        on_status=writer.on_status,
        on_spawn=lambda: None,
        on_down=lambda: _log("filter child down; FilterLink will respawn it"),
        fresh_s=1.0,
    )
    link.start()
    link.reset(_EPOCH)

    stop = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()

    period = 1.0 / args.fps
    offset, direction = 0, 1
    try:
        while not stop.is_set():
            t0 = time.monotonic()
            link.submit(scene.crop(offset), t0, _EPOCH)
            offset, direction = _bounce(offset, direction)
            remaining = period - (time.monotonic() - t0)
            if remaining > 0:
                stop.wait(remaining)
    finally:
        link.stop()
        writer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
