"""`ffsb-camera-filter-preview`: the Control Panel applet's preview
helper. Spawns the same `ffsb-camera-filter` binary the feeder uses,
pointed at a settings file the applet rewrites on every pending edit,
and feeds it a canned, slowly panning test scene at 10 fps, so it works
while the lane is locked. Each filtered output is written as a PNG the
applet's `Image` element can show. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"Preview".

With `--camera` (the sheet's "Use camera" box) the frames come from the
real camera instead: an ordinary `pipewiresrc` that WirePlumber's policy
links to the default camera, never tagged `firefox-sandbox.lane` (the
capture script would take it for the sandbox feeder). The newest camera
frame per tick is submitted, so the PNGs stay at --fps. A pipeline error,
or no camera frame for `_CAMERA_TIMEOUT_S` (at start or mid-stream),
releases the camera and falls back to the scene. stdout says which source is live: `source
camera` once the first camera frame is taken, `source scene` at start
without --camera or on the fallback, before that source's first
`frame-<n>.png <ms>` line (a camera frame still in the child when the
camera fails mid-stream may print just after `source scene`).

No still outlives the helper: stale `frame-*.png` in --out are swept at
start, and the writer deletes the frames it kept when it stops.

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

# --camera: no camera frame this long, after start (none linked, or one
# that never negotiates) or since the last one, means no camera, and the
# scene takes over. The applet's own stale-preview watchdog allows for it
# (PreviewRunner.qml).
_CAMERA_TIMEOUT_S = 5.0
# CameraSource.stop()'s bound on releasing the camera: cancelling a start
# still waiting for its link, then the state change to NULL.
_CAMERA_STOP_S = 3.0
_CAMERA_CANCEL_RETRY_S = 0.05

# No `firefox-sandbox.lane` in the stream properties, and autoconnect:
# WirePlumber's normal policy links it to the default camera, as for any
# other application. application.name is the tray's label for it.
CAMERA_PIPELINE = (
    "pipewiresrc name=src autoconnect=true client-name=ffsb-camera-preview "
    'stream-properties="props,application.name=(string)\\"Camera Properties preview\\"" ! '
    "videoconvert ! videoscale ! "
    f"video/x-raw,format=NV12,width={OUT_W},height={OUT_H} ! "
    "appsink name=sink max-buffers=1 drop=true sync=false"
)

_stdout_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"ffsb-camera-filter-preview: {msg}", file=sys.stderr, flush=True)


def _say(line: str) -> None:
    """One whole stdout line: the writer thread and the submit loop both
    print, and `print` writes the text and its newline separately."""
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


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
        """Writes what is pending, then deletes every frame it kept."""
        with self._cv:
            self._stopped = True
            self._cv.notify_all()
        self._thread.join()
        with self._lock:
            kept, self._kept = self._kept, []
        for n in kept:
            try:
                (self._out_dir / f"frame-{n}.png").unlink()
            except FileNotFoundError:
                pass

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
        _say(f"frame-{n}.png {ms}")
        for old in stale:
            try:
                (self._out_dir / f"frame-{old}.png").unlink()
            except FileNotFoundError:
                pass


class CameraSource:
    """The real camera: `CAMERA_PIPELINE` (or `make_pipeline()`, a test
    seam) run on its own daemon thread, which starts it, pulls samples
    and watches the bus. Each frame goes to `on_frame(nv12, arrival_t)`,
    and the first ERROR or EOS to `on_error(text)` -- both on that
    thread, so they must only marshal. Nothing is delivered once `stop()`
    has begun.

    The start to PLAYING may block while pipewiresrc waits for its link
    and a format (pwinput.py's module docstring). `stop()` cancels such a
    start with flush-start events on the source pad, as
    pwinput.Input._cancel_start does, then sets the pipeline to NULL,
    which releases the camera. Both are bounded by `_CAMERA_STOP_S`: a
    start that will not cancel is logged and abandoned to its daemon
    thread, and process exit closes its PipeWire connection anyway."""

    def __init__(self, on_frame, on_error, make_pipeline=None) -> None:
        self._on_frame = on_frame
        self._on_error = on_error
        self._make_pipeline = make_pipeline or (lambda: Gst.parse_launch(CAMERA_PIPELINE))
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._start_done = threading.Event()
        self._pipeline = None
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ffsb-preview-camera", daemon=True)
        self._thread.start()

    def _deliver_error(self, text: str) -> None:
        if not self._halt.is_set():
            self._on_error(text)

    def _run(self) -> None:
        try:
            if not Gst.is_initialized():
                Gst.init(None)
            pipeline = self._make_pipeline()
        except Exception as e:
            self._start_done.set()
            self._deliver_error(str(e))
            return
        with self._lock:
            if self._halt.is_set():
                self._start_done.set()
                return
            self._pipeline = pipeline
        try:
            self._play(pipeline)
        except Exception as e:  # a dead thread must not wait out the timeout
            self._start_done.set()
            self._deliver_error(f"camera thread failed: {e!r}")

    def _play(self, pipeline) -> None:
        bus = pipeline.get_bus()
        try:
            ret = pipeline.set_state(Gst.State.PLAYING)
        finally:
            self._start_done.set()
        if ret == Gst.StateChangeReturn.FAILURE:
            self._deliver_error(self._bus_text(bus) or "state change to PLAYING failed")
            return
        sink = pipeline.get_by_name("sink")
        while not self._halt.is_set():
            sample = sink.emit("try-pull-sample", int(_LOAD_POLL_S * Gst.SECOND))
            if sample is not None and not self._halt.is_set():
                buf = sample.get_buffer()
                ok, mapinfo = buf.map(Gst.MapFlags.READ)
                if ok:
                    try:
                        self._on_frame(bytes(mapinfo.data), time.monotonic())
                    finally:
                        buf.unmap(mapinfo)
            text = self._bus_text(bus)
            if text is not None:
                self._deliver_error(text)
                return

    @staticmethod
    def _bus_text(bus) -> str | None:
        msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return None
        if msg.type == Gst.MessageType.EOS:
            return "the camera stream ended (EOS)"
        err, debug = msg.parse_error()
        return f"{err}: {debug}" if debug else str(err)

    def stop(self) -> None:
        with self._lock:
            self._halt.set()
            pipeline = self._pipeline
        if pipeline is None:
            return
        give_up = time.monotonic() + _CAMERA_STOP_S
        src = pipeline.get_by_name("src")
        pad = src.get_static_pad("src") if src is not None else None
        while pad is not None and not self._start_done.is_set() and time.monotonic() < give_up:
            pad.send_event(Gst.Event.new_flush_start())
            self._start_done.wait(_CAMERA_CANCEL_RETRY_S)
        to_null = threading.Thread(target=pipeline.set_state, args=(Gst.State.NULL,),
                                   name="ffsb-preview-camera-stop", daemon=True)
        to_null.start()
        to_null.join(max(0.0, give_up - time.monotonic()))
        if to_null.is_alive():
            _log(f"camera pipeline still stopping after {_CAMERA_STOP_S:g} s; abandoning it")
            return
        if self._thread is not None:
            self._thread.join(max(0.0, give_up - time.monotonic()))


class _CameraSlot:
    """The camera's newest frame and first error, handed from its
    streaming thread to the submit loop: newest wins, nothing queues."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: tuple[bytes, float] | None = None
        self._error: str | None = None

    def on_frame(self, frame: bytes, t: float) -> None:
        with self._lock:
            self._frame = (frame, t)

    def on_error(self, text: str) -> None:
        with self._lock:
            if self._error is None:
                self._error = text

    def take(self) -> tuple[tuple[bytes, float] | None, str | None]:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame, self._error


def _watch_stdin(stop: threading.Event, fd: int = 0) -> None:
    """Sets `stop` on stdin EOF (the sheet closed). Read on its own
    thread so the 10 fps submit loop below is never blocked on it."""
    try:
        while True:
            if not os.read(fd, 4096):
                break
    except OSError:
        pass
    stop.set()


def main(argv: list[str] | None = None, *, stdin_fd: int = 0, make_camera=CameraSource,
         camera_timeout_s: float = _CAMERA_TIMEOUT_S) -> int:
    """`stdin_fd`, `make_camera(on_frame, on_error)` and
    `camera_timeout_s` are test seams: a pipe for the sheet's stdin, a
    fake camera, a short no-frame timeout."""
    parser = argparse.ArgumentParser(
        prog="ffsb-camera-filter-preview", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--settings", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--image")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--camera", action="store_true",
                        help="preview the real camera, falling back to the scene")
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else preview_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    # A helper killed outright (no stop()) leaves its last frames behind.
    for stale in out_dir.glob("frame-*.png"):
        stale.unlink(missing_ok=True)

    # The child reads its settings from its own environment: inherited by
    # every subprocess FilterLink spawns below, same process for this
    # helper's whole life.
    os.environ["FFSB_CAMERA_SETTINGS"] = str(Path(args.settings))

    camera = slot = None
    scene = None
    if args.camera:
        slot = _CameraSlot()
        camera = make_camera(slot.on_frame, slot.on_error)
    else:
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
    threading.Thread(target=_watch_stdin, args=(stop, stdin_fd), daemon=True).start()

    period = 1.0 / args.fps
    offset, direction = 0, 1
    live = False  # a camera frame has been taken
    try:
        if camera is not None:
            camera.start()
            # Start-up's deadline, then each frame's arrival plus the timeout.
            camera_deadline = time.monotonic() + camera_timeout_s
        else:
            _say("source scene")
        while not stop.is_set():
            t0 = time.monotonic()
            if camera is not None:
                frame, error = slot.take()
                if frame is not None:
                    camera_deadline = t0 + camera_timeout_s
                elif error is None and t0 >= camera_deadline:
                    error = (f"no frame for {camera_timeout_s:g} s" if live
                             else f"no frame within {camera_timeout_s:g} s")
                if error is not None:
                    # `source scene` first: the applet's watchdog then
                    # never waits on a slow cancel or image load.
                    _log(f"camera unavailable: {error}, using test scene")
                    _say("source scene")
                    camera.stop()
                    camera = None
                    scene = _Scene(_load_scene(args.image))
                elif frame is not None:
                    if not live:
                        live = True
                        _say("source camera")
                    link.submit(frame[0], frame[1], _EPOCH)
            if scene is not None:
                link.submit(scene.crop(offset), t0, _EPOCH)
                offset, direction = _bounce(offset, direction)
            remaining = period - (time.monotonic() - t0)
            if remaining > 0:
                stop.wait(remaining)
    finally:
        if camera is not None:
            camera.stop()
        link.stop()
        writer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
