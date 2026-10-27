"""`ffsb-camera-filter`: the filter child. A pure stdin -> stdout filter
over the protocol in `protocol.py`, driving a `Chain` (`chain.py`). Never
opens a device or talks to PipeWire; exits on stdin EOF. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
filter child", and the plan's Global Constraints for the behaviour pinned
here.
"""
from __future__ import annotations

import json
import os
import random
import select
import struct
import sys
import time
from pathlib import Path
from typing import BinaryIO

from .chain import Chain
from .protocol import (
    FRAME,
    HOLD_EMPTY,
    HOLD_GRAB,
    RELEASE,
    RESET,
    Parser,
    decode_frame_in,
    encode_frame_out,
    encode_status,
)
from .settings import Knobs, SettingsError, builtin_presets_path, load_presets, resolve, state_dir

_EMA_ALPHA = 0.1


def _log(msg: str) -> None:
    print(f"ffsb-camera-filter: {msg}", file=sys.stderr, flush=True)


def _guess_preset_id(text: str | None) -> str:
    """The preset id named in a broken `filter.json`, if the JSON itself
    at least parses; `""` otherwise. Used only for STATUS's `preset`
    field while `ok` is false."""
    if text is None:
        return ""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return ""
    preset_id = doc.get("preset") if isinstance(doc, dict) else None
    return preset_id if isinstance(preset_id, str) else ""


def run(
    in_fd: int,
    out: BinaryIO,
    settings_path: Path,
    user_presets_path: Path,
    builtin_path: Path,
    clock=time.monotonic,
    rng=None,
) -> int:
    """Runs until stdin EOF, returning 0. `out` gets every FRAME and
    STATUS message this process emits."""
    chain = Chain(Knobs(), rng if rng is not None else random.Random())
    delay_ms = int(os.environ.get("FFSB_CAMERA_FILTER_DELAY_MS", "0") or 0)
    parser = Parser()

    stored_epoch: int | None = None
    ok = True
    preset_id = "none"
    modified = False
    error_text: str | None = None
    ema_ms = 0.0

    last_stat = object()  # never equal to a real stat pair: forces the first check
    last_sent_cmp: dict | None = None
    last_sent_time = clock()

    def _stat(path: Path):
        try:
            s = path.stat()
            return (s.st_ino, s.st_mtime_ns)
        except OSError:
            return None

    def reload_if_changed() -> bool:
        nonlocal last_stat, ok, preset_id, modified, error_text
        current = (_stat(settings_path), _stat(user_presets_path))
        if current == last_stat:
            return False
        last_stat = current
        text = settings_path.read_text() if settings_path.exists() else None
        try:
            presets, skipped = load_presets(builtin_path, user_presets_path)
            resolved = resolve(text, presets, skipped)
        except SettingsError as e:
            ok, error_text, preset_id = False, str(e), _guess_preset_id(text)
            _log(f"settings error: {error_text}")
            return True
        chain.set_knobs(resolved.knobs)
        ok, error_text, preset_id, modified = True, None, resolved.preset_id, resolved.modified
        return True

    def status_dict() -> dict:
        d = {
            "preset": preset_id,
            "modified": modified,
            "ok": ok,
            "holding": chain.holding,
            "ms_per_frame": ema_ms,
        }
        if not ok:
            d["error"] = error_text or ""
        return d

    def send_status(*, force: bool) -> None:
        nonlocal last_sent_cmp, last_sent_time
        d = status_dict()
        cmp_fields = {k: v for k, v in d.items() if k != "ms_per_frame"}
        if force or cmp_fields != last_sent_cmp:
            out.write(encode_status(d))
            out.flush()
            last_sent_cmp = cmp_fields
            last_sent_time = clock()

    reload_if_changed()
    send_status(force=True)

    while True:
        ready, _, _ = select.select([in_fd], [], [], 1.0)
        if not ready:
            if reload_if_changed():
                send_status(force=True)
            continue

        data = os.read(in_fd, 1 << 20)
        if not data:
            return 0

        force_status = False
        frames_seen = False
        for kind, payload in parser.feed(data):
            if kind == FRAME:
                if reload_if_changed():
                    force_status = True
                frames_seen = True
                epoch, pts_ns, frame = decode_frame_in(payload)
                if epoch == stored_epoch and ok:
                    t0 = clock()
                    result = chain.process(frame, pts_ns)
                    ema_ms = ema_ms * (1 - _EMA_ALPHA) + (clock() - t0) * 1000 * _EMA_ALPHA
                    if result is not None:
                        if delay_ms:
                            time.sleep(delay_ms / 1000)
                        out.write(encode_frame_out(epoch, pts_ns, result.held, result.frame))
                        out.flush()
            elif kind == HOLD_GRAB:
                chain.hold_grab()
                force_status = True
            elif kind == HOLD_EMPTY:
                chain.hold_empty()
                force_status = True
            elif kind == RELEASE:
                chain.release()
                force_status = True
            elif kind == RESET:
                (stored_epoch,) = struct.unpack("<I", payload)
                chain.reset()
                force_status = True

        if force_status:
            send_status(force=True)
        elif frames_seen and clock() - last_sent_time >= 1.0:
            send_status(force=True)


def main() -> int:
    settings_override = os.environ.get("FFSB_CAMERA_SETTINGS")
    settings_path = Path(settings_override) if settings_override else state_dir() / "filter.json"
    return run(
        0,
        sys.stdout.buffer,
        settings_path,
        state_dir() / "presets.json",
        builtin_presets_path(),
    )


if __name__ == "__main__":
    sys.exit(main())
