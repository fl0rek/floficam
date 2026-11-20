"""Render the README media from a source clip: one looping animated WebP
per built-in preset, a side-by-side grid video of all of them, and the
splash image.

Every frame goes through the same `Chain` the daemon runs, fed 1280x720
NV12 at 30 fps with the clip's own timestamps, so fps caps, stalls and
datamosh behave as they do live. The rng is seeded, so a re-run on the
same clip gives the same media.

Run it through the flake, which supplies ffmpeg, the font and the
GStreamer plugins the codec presets need:

    nix run .#render-demos -- SOURCE [--start S] [--duration S] [--still S]

The checked-in media came from Tears of Steel (CC BY 3.0, Blender
Foundation, mango.blender.org), 6:20 to 6:26, centre-cropped to 16:9:

    nix run .#render-demos -- tears_of_steel_1080p.webm --start 380 --duration 6 --still 1
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from floficam.filter.chain import H, W, Chain
from floficam.filter.settings import builtin_presets_path, load_presets

FPS = 30
FRAME_NS = 1_000_000_000 // FPS
FRAME_BYTES = W * H * 3 // 2

TILE_W, TILE_H = 480, 270  # per-preset clips and grid tiles
CLIP_FPS = 10
SPLASH_W, SPLASH_H = 1280, 640
BG = "0x008080"  # the old desktop teal


def ffmpeg(*args: str, stdin=None, stdout=None) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        stdin=stdin,
        stdout=stdout,
    )


def run(*args: str) -> None:
    proc = ffmpeg(*args)
    if proc.wait() != 0:
        sys.exit(f"ffmpeg failed: {' '.join(args)}")


def label(text: str) -> str:
    """`text` escaped for a drawtext `text=` value inside a filtergraph."""
    for ch in "\\':,;[]":
        text = text.replace(ch, "\\" + ch)
    return text


def decode(src: str, start: float, duration: float, crop: str) -> subprocess.Popen:
    vf = f"{crop + ',' if crop else ''}scale={W}:{H}:flags=lanczos,fps={FPS}"
    return ffmpeg(
        "-ss", str(start), "-i", src, "-t", str(duration), "-an",
        "-vf", vf, "-pix_fmt", "nv12", "-f", "rawvideo", "-",
        stdout=subprocess.PIPE,
    )


def render_preset(knobs, args, out: Path) -> None:
    """Feed the clip through one preset's Chain into a near-lossless
    intermediate at full size."""
    dec = decode(args.source, args.start, args.duration, args.crop)
    enc = ffmpeg(
        "-f", "rawvideo", "-pix_fmt", "nv12", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p", str(out),
        stdin=subprocess.PIPE,
    )
    chain = Chain(knobs, np.random.default_rng(0))
    last = None
    try:
        i = 0
        while True:
            frame = dec.stdout.read(FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                break
            result = chain.process(frame, i * FRAME_NS)
            last = result.frame if result is not None else (last or frame)
            enc.stdin.write(last)
            i += 1
    finally:
        chain.close()
        enc.stdin.close()
        dec.stdout.close()
    if dec.wait() != 0 or enc.wait() != 0 or i == 0:
        sys.exit(f"rendering {out.name} failed ({i} frames)")


def tile(name: str, font: str, w: int, h: int, size: int) -> str:
    """Scale to w x h and stamp the preset name in the bottom-left corner."""
    return (
        f"scale={w}:{h}:flags=area,"
        f"drawtext=fontfile={font}:text='{label(name)}':fontsize={size}:fontcolor=white:"
        f"box=1:boxcolor=black@0.7:boxborderw=6:x=8:y=h-th-14"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="render-demos", description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="any video ffmpeg can read")
    ap.add_argument("--start", type=float, default=0.0, help="clip start in the source, seconds")
    ap.add_argument("--duration", type=float, default=6.0, help="clip length, seconds")
    ap.add_argument("--still", type=float, default=1.0, help="splash frame, seconds into the clip")
    ap.add_argument("--crop", default="crop=ih*16/9:ih",
                    help="ffmpeg filter applied before scaling to 1280x720 ('' for none)")
    ap.add_argument("--out", type=Path, default=Path("docs/media"))
    ap.add_argument("--icon", type=Path, default=Path("docs/media/floficam-icon.png"))
    ap.add_argument("--font", default=os.environ.get("FLOFICAM_DEMO_FONT"),
                    help="a TTF/OTF for labels and the wordmark")
    args = ap.parse_args(argv)
    if not args.font:
        ap.error("--font (or FLOFICAM_DEMO_FONT) is required")
    font = args.font.replace(":", "\\:")

    presets, _ = load_presets(builtin_presets_path(), None)
    args.out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="floficam-demos-"))
    try:
        for pid, preset in presets.items():
            print(f"rendering {pid}", flush=True)
            render_preset(preset.knobs, args, tmp / f"{pid}.mp4")
            run(
                "-i", str(tmp / f"{pid}.mp4"),
                "-vf", f"fps={CLIP_FPS},scale={TILE_W}:{TILE_H}:flags=area",
                "-c:v", "libwebp", "-quality", "60", "-compression_level", "6", "-loop", "0",
                str(args.out / f"preset-{pid}.webp"),
            )
            run("-ss", str(args.still), "-i", str(tmp / f"{pid}.mp4"), "-frames:v", "1",
                str(tmp / f"{pid}.png"))

        names = {pid: ("Original" if pid == "none" else p.name) for pid, p in presets.items()}
        ids = list(presets)
        cols = 4

        # All presets side by side, full motion.
        inputs, chains = [], []
        for n, pid in enumerate(ids):
            inputs += ["-i", str(tmp / f"{pid}.mp4")]
            chains.append(f"[{n}:v]{tile(names[pid], font, TILE_W, TILE_H, 32)}[t{n}]")
        layout = "|".join(f"{(n % cols) * TILE_W}_{(n // cols) * TILE_H}" for n in range(len(ids)))
        graph = ";".join(chains) + ";" + "".join(f"[t{n}]" for n in range(len(ids))) + (
            f"xstack=inputs={len(ids)}:layout={layout}:fill=black"
        )
        run(*inputs, "-filter_complex", graph, "-c:v", "libx264", "-crf", "26", "-preset", "slow",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.out / "presets.mp4"))

        # Splash: icon and wordmark over teal, the still through every preset below.
        tw, th, gap = 272, 153, 24
        x0 = (SPLASH_W - (cols * tw + (cols - 1) * gap)) // 2
        y0 = 248
        inputs = ["-f", "lavfi", "-i", f"color=c={BG}:s={SPLASH_W}x{SPLASH_H}:d=1",
                  "-i", str(args.icon)]
        chains = [
            "[1:v]scale=iw*4:ih*4:flags=neighbor[icon]",
            "[0:v][icon]overlay=x=64:y=56,"
            f"drawtext=fontfile={font}:text='floficam':fontsize=96:fontcolor=white:x=224:y=64,"
            f"drawtext=fontfile={font}:text='lo-fi effects for your webcam':fontsize=32:"
            "fontcolor=white:x=228:y=176[bg0]",
        ]
        for n, pid in enumerate(ids):
            inputs += ["-i", str(tmp / f"{pid}.png")]
            x = x0 + (n % cols) * (tw + gap)
            y = y0 + (n // cols) * (th + gap + 8)
            chains.append(f"[{n + 2}:v]{tile(names[pid], font, tw, th, 16)},"
                          f"pad={tw + 4}:{th + 4}:2:2:black[s{n}]")
            chains.append(f"[bg{n}][s{n}]overlay=x={x - 2}:y={y - 2}[bg{n + 1}]")
        graph = ";".join(chains)
        run(*inputs, "-filter_complex", graph, "-map", f"[bg{len(ids)}]", "-frames:v", "1",
            str(args.out / "splash.png"))
    finally:
        shutil.rmtree(tmp)
    for f in sorted(args.out.iterdir()):
        print(f"{f}  {f.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
