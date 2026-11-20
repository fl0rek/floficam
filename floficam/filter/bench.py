"""`floficam-filter-bench`: ms per frame for every built-in preset, in
process. No camera, PipeWire or feeder: each preset runs through a fresh
`Chain` over a canned, moving 1280x720 NV12 sequence with pts at 30 fps,
so the fps cap and stalls cut in as they would live. Prints
`<preset-id> <mean ms>` per line, in preset order, and exits 1 if any
mean exceeds `--max-ms` (100 by default: half of FRESH_S: slower than
that, live output stutters (held frames by default, bars with --hold 0)).
See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"Testing".
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from .chain import H, W, Chain
from .settings import builtin_presets_path, load_presets

_DISTINCT_FRAMES = 30  # one second of motion, cycled
_FRAME_NS = 1_000_000_000 // 30


def moving_frames(n: int) -> list[bytes]:
    """`n` NV12 frames: a diagonal gradient scrolling right, a bright
    block sliding across it, and a chroma ramp that drifts down."""
    yy, xx = np.mgrid[0:H, 0:W]
    frames = []
    for i in range(n):
        y = ((xx + yy - 12 * i) % 220 + 16).astype(np.uint8)
        bx = (37 * i) % (W - 192)
        by = H // 3 + (11 * i) % (H // 3)
        y[by : by + 192, bx : bx + 192] = 235
        uv = np.empty((H // 2, W // 2, 2), dtype=np.uint8)
        uv[:, :, 0] = (np.arange(W // 2) * 200 // (W // 2) + 28)[None, :]
        uv[:, :, 1] = ((np.arange(H // 2) + 4 * i) % 200 + 28)[:, None]
        frames.append(y.tobytes() + uv.tobytes())
    return frames


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="floficam-filter-bench", description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--max-ms", type=float, default=100.0)
    args = parser.parse_args(argv)

    presets, _ = load_presets(builtin_presets_path(), None)
    frames = moving_frames(min(args.frames, _DISTINCT_FRAMES))

    over = False
    for preset_id, preset in presets.items():
        chain = Chain(preset.knobs, np.random.default_rng(0))
        total = 0.0
        try:
            for i in range(args.frames):
                t0 = time.perf_counter()
                chain.process(frames[i % len(frames)], i * _FRAME_NS)
                total += time.perf_counter() - t0
        finally:
            chain.close()
        mean_ms = total * 1000 / args.frames
        over |= mean_ms > args.max_ms
        print(f"{preset_id} {mean_ms:.2f}", flush=True)
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
