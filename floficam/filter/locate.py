"""Where to find the filter child from the feeder. See the plan's Global
Constraints: "The child is located as a sibling of the running entry
point ... never PATH." Under the wrapGApps wrapper, `argv0` is
`$out/bin/.ffsb-camera-feeder-wrapped`; its sibling `ffsb-camera-filter`
is the child's own wrapper, so resolving relative to argv0 reaches the
wrapped child either way.
"""
from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path


def child_argv(env: Mapping[str, str] = os.environ, argv0: str = sys.argv[0]) -> list[str]:
    """`FFSB_CAMERA_FILTER` (shlex-split) is a test/harness override only;
    otherwise the sibling `ffsb-camera-filter` next to the running entry
    point."""
    override = env.get("FFSB_CAMERA_FILTER")
    if override:
        return shlex.split(override)
    return [str(Path(argv0).resolve().parent / "ffsb-camera-filter")]
