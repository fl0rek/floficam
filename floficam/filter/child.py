"""`floficam-filter`: the filter child. A pure stdin -> stdout filter
over the protocol in `protocol.py`, driving a `Chain` (`chain.py`). Never
opens a device or talks to PipeWire; exits on stdin EOF. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
filter child", and the plan's Global Constraints for the behaviour pinned
here.
"""
from __future__ import annotations

import json
import os
import select
import sys
import time
from pathlib import Path
from typing import BinaryIO

import numpy as np

from .chain import H, W, Chain, ChainConfigError
from .protocol import (
    FRAME,
    HOLD_EMPTY,
    HOLD_GRAB,
    RELEASE,
    RESET,
    Parser,
    decode_frame_in,
    decode_reset,
    encode_frame_out,
    encode_status,
)
from .settings import Knobs, SettingsError, builtin_presets_path, load_presets, resolve, state_dir

_EMA_ALPHA = 0.1
# 1280x720 NV12. Any other input length is dropped, never fed to the
# chain: its pixel stages can't take it, and one bad frame must not
# take the child down.
FRAME_BYTES = W * H * 3 // 2


def _log(msg: str) -> None:
    print(f"floficam-filter: {msg}", file=sys.stderr, flush=True)


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
    chain = Chain(Knobs(), rng if rng is not None else np.random.default_rng())
    delay_ms = int(os.environ.get("FLOFICAM_FILTER_DELAY_MS", "0") or 0)
    parser = Parser()

    stored_epoch: int | None = None
    ok = True
    preset_id = "none"
    modified = False
    error_text: str | None = None
    ema_ms = 0.0
    bad_lengths: set[int] = set()  # logged once each

    last_stat = object()  # never equal to a real stat pair: forces the first check
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
        text = None
        try:
            try:
                text = settings_path.read_text()
            except FileNotFoundError:
                pass  # no filter.json: preset `none`
            presets, skipped = load_presets(builtin_path, user_presets_path)
            resolved = resolve(text, presets, skipped)
            chain.set_knobs(resolved.knobs)
        except (SettingsError, ChainConfigError) as e:
            error = str(e)
        except (OSError, UnicodeDecodeError) as e:
            error = f"{settings_path.name}: unreadable: {e}"
        except Exception as e:
            # Fail closed on whatever else a hand-edited file trips:
            # a crash here would respawn into the same crash, forever.
            error = f"unexpected: {e!r}"
        else:
            error = None
        if error is not None:
            ok, error_text, preset_id = False, error, _guess_preset_id(text)
            _log(f"settings error: {error_text}")
            return True
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

    def send_status() -> None:
        nonlocal last_sent_time
        out.write(encode_status(status_dict()))
        out.flush()
        last_sent_time = clock()

    reload_if_changed()
    send_status()

    while True:
        ready, _, _ = select.select([in_fd], [], [], 1.0)
        if not ready:
            if reload_if_changed():
                send_status()
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
                if len(frame) != FRAME_BYTES:
                    if len(frame) not in bad_lengths:
                        bad_lengths.add(len(frame))
                        _log(f"dropping input frames of {len(frame)} bytes, want {FRAME_BYTES}")
                    continue
                if epoch == stored_epoch and ok:
                    holding_before = chain.holding
                    t0 = clock()
                    result = chain.process(frame, pts_ns)
                    ema_ms = ema_ms * (1 - _EMA_ALPHA) + (clock() - t0) * 1000 * _EMA_ALPHA
                    if chain.holding != holding_before:
                        # A HOLD_GRAB's capture: "empty" -> "frame" on this
                        # very call. The feeder should see that promptly,
                        # not wait for the next heartbeat.
                        force_status = True
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
                stored_epoch = decode_reset(payload)
                try:
                    chain.reset()
                except ChainConfigError as e:
                    # The codec stage failed to rebuild: fail closed
                    # (the chain emits nothing) and say so, as for a
                    # settings error, until a settings write fixes it.
                    ok, error_text = False, str(e)
                    _log(f"settings error: {error_text}")
                force_status = True

        if force_status:
            send_status()
        elif frames_seen and clock() - last_sent_time >= 1.0:
            send_status()


def main() -> int:
    settings_override = os.environ.get("FLOFICAM_SETTINGS")
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
