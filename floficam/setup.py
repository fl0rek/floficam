"""`floficam setup`: create the virtual camera device."""
import fcntl
import os
import re
import shutil
import struct
import subprocess
import sys
import time

from .device import LABEL, find_device

CTL_PATH = "/dev/v4l2loopback"
CTL_ADD = 0x40487E01  # _IOW('~', 1, struct v4l2_loopback_config)
MISSING = ("v4l2loopback is not installed: install your distribution's v4l2loopback package, "
           "or on NixOS set programs.floficam.loopback.enable = true")


# modprobe joins its parameters and the kernel splits them on spaces, so a
# label must never carry one (`x max_buffers=99` would set a parameter).
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,31}")
BAD_LABEL = "label must be 1 to 31 characters of A-Z, a-z, 0-9, _ and -"


def pack_config(label: str) -> bytearray:
    raw = label.encode()
    if len(raw) > 31:
        raise ValueError("label is longer than 31 bytes")
    return bytearray(struct.pack("<ii32sIIIIiiii", -1, -1, raw, 0, 0, 0, 0, 2, -1, 0, 1))


def _ctl_add(buf: bytearray) -> int:
    fd = os.open(CTL_PATH, os.O_RDWR)
    try:
        return fcntl.ioctl(fd, CTL_ADD, buf, True)
    finally:
        os.close(fd)


def _modprobe(args) -> tuple[int, str]:
    p = subprocess.run([shutil.which("modprobe") or "/sbin/modprobe", *args],
                       capture_output=True, text=True)
    return p.returncode, p.stderr


def _wait_for(predicate, timeout) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _sudo_reexec(argv):
    os.execvp(argv[0], argv)


def run_setup(label: str = LABEL, *, find=find_device,
              is_root=lambda: os.geteuid() == 0,
              ctl_exists=lambda: os.path.exists(CTL_PATH),
              ctl_add=_ctl_add, modprobe=_modprobe, reexec=_sudo_reexec,
              wait=_wait_for, out=print) -> int:
    if not _LABEL.fullmatch(label):
        out(f"{BAD_LABEL}, got {label!r}")
        return 1
    existing = find(None, label)
    if existing:
        out(f"floficam device already exists: {existing}")
        return 0
    if not is_root():
        argv = ["sudo", "--", os.path.realpath(sys.argv[0]), "setup", "--label", label]
        out(f"floficam setup needs root, running: {' '.join(argv)}")
        reexec(argv)
        return 1
    timed_out = f"the module accepted the request but no device named {label} appeared"
    if ctl_exists():
        try:
            n = ctl_add(pack_config(label))
        except OSError as e:
            out(f"could not add a device: {e.strerror}")
            return 1
        path = f"/dev/video{n}"
        if not wait(lambda: os.path.exists(path), 5):
            out(timed_out)
            return 1
        out(f"created {path}")
        return 0
    code, err = modprobe(["v4l2loopback", "devices=1", f"card_label={label}",
                          "exclusive_caps=0", "max_buffers=2"])
    if code:
        out(MISSING if "not found" in err else f"modprobe failed: {err.strip()}")
        return 1
    if not wait(lambda: find(None, label) is not None, 5):
        out(timed_out)
        return 1
    out(f"created {find(None, label)}")
    return 0
