"""Finding the v4l2loopback device by its label."""
from pathlib import Path

LABEL = "floficam"
NO_DEVICE = ("no floficam device: create one with `floficam setup`, "
             "or enable programs.floficam.loopback on NixOS")


def find_device(explicit: str | None, label: str = LABEL, *,
                sysfs: Path = Path("/sys/class/video4linux"), dev: Path = Path("/dev")) -> str | None:
    """`explicit` as given (even when missing, so the open error names it);
    else the `/dev/floficam` symlink (default label only); else the first
    video4linux entry whose name is `label`; else None."""
    if explicit:
        return explicit
    if label == LABEL and (dev / LABEL).exists():
        return str(dev / LABEL)
    try:
        entries = sorted(sysfs.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for entry in entries:
        try:
            if (entry / "name").read_text().strip() == label:
                return str(dev / entry.name)
        except OSError:
            continue
    return None
