import errno

import pytest

from floficam.setup import CTL_ADD, pack_config, run_setup

HINT = ("v4l2loopback is not installed: install your distribution's v4l2loopback package, "
        "or on NixOS set programs.floficam.loopback.enable = true")


def test_constants_and_layout():
    assert CTL_ADD == 0x40487E01
    buf = pack_config("floficam")
    assert len(buf) == 72 and buf[8:17] == b"floficam\0"
    i32 = lambda o: int.from_bytes(buf[o:o + 4], "little", signed=True)
    assert (i32(0), i32(4), i32(56), i32(60), i32(64), i32(68)) == (-1, -1, 2, -1, 0, 1)
    with pytest.raises(ValueError):
        pack_config("x" * 32)


def test_exists_is_a_no_op():
    calls = []
    assert run_setup(find=lambda *_: "/dev/video3", ctl_add=calls.append, out=calls.append) == 0
    assert calls == ["floficam device already exists: /dev/video3"]


def test_not_root_reexecs_with_label():
    got, lines = [], []
    run_setup("cam", find=lambda *_: None, is_root=lambda: False, reexec=got.append, out=lines.append)
    assert len(got) == 1 and got[0][:2] == ["sudo", "--"] and got[0][3:] == ["setup", "--label", "cam"]
    assert lines[0].startswith("floficam setup needs root, running: sudo ")


def test_ctl_add_then_wait():
    lines, bufs = [], []

    def add(buf):
        bufs.append(buf)
        return 11

    assert run_setup(find=lambda *_: None, is_root=lambda: True, ctl_exists=lambda: True,
                     ctl_add=add, wait=lambda p, t: True, out=lines.append) == 0
    assert lines == ["created /dev/video11"] and len(bufs[0]) == 72


def test_ctl_add_error_reports_errno():
    def boom(_):
        raise OSError(errno.ENOSPC, "No space left on device")
    lines = []
    assert run_setup(find=lambda *_: None, is_root=lambda: True, ctl_exists=lambda: True,
                     ctl_add=boom, out=lines.append) == 1
    assert lines[-1] == "could not add a device: No space left on device"


def _modprobe_run(result, wait=lambda p, t: True, find=lambda *_: None):
    lines, args = [], []

    def mp(a):
        args.append(a)
        return result

    rc = run_setup(find=find, is_root=lambda: True, ctl_exists=lambda: False,
                   modprobe=mp, wait=wait, out=lines.append)
    return rc, lines, args


def test_modprobe_success():
    paths = iter([None, "/dev/video0"])
    rc, lines, args = _modprobe_run((0, ""), find=lambda *_: next(paths))
    assert rc == 0 and lines == ["created /dev/video0"]
    assert args == [["v4l2loopback", "devices=1", "card_label=floficam", "exclusive_caps=0", "max_buffers=2"]]


def test_modprobe_missing_module_hint():
    rc, lines, _ = _modprobe_run((1, "modprobe: FATAL: Module v4l2loopback not found in directory /lib/modules/x\n"))
    assert rc == 1 and lines == [HINT]


def test_modprobe_other_failure_shows_stderr():
    rc, lines, _ = _modprobe_run((1, "modprobe: ERROR: could not insert 'v4l2loopback': Key was rejected by service\n"))
    assert rc == 1 and "Key was rejected" in lines[-1] and lines[-1].startswith("modprobe failed: ")
    assert HINT not in lines


def test_device_never_appears():
    rc, lines, _ = _modprobe_run((0, ""), wait=lambda p, t: False)
    assert rc == 1 and lines == ["the module accepted the request but no device named floficam appeared"]
    lines = []
    assert run_setup(find=lambda *_: None, is_root=lambda: True, ctl_exists=lambda: True,
                     ctl_add=lambda b: 5, wait=lambda p, t: False, out=lines.append) == 1
    assert lines == ["the module accepted the request but no device named floficam appeared"]


@pytest.mark.parametrize("label", ["x max_buffers=99", "", "a" * 32, "a,b", "a=b", "café", "a\tb"])
def test_label_charset_refused_before_anything_runs(label):
    """modprobe joins its parameters and the kernel splits them on spaces:
    a label with a space would inject module parameters. Refused before
    the device lookup, the sudo re-exec, the ioctl or modprobe."""
    calls, lines = [], []
    rc = run_setup(label, find=lambda *a: calls.append("find"), is_root=lambda: True,
                   ctl_exists=lambda: False, ctl_add=calls.append, modprobe=lambda a: calls.append(a) or (0, ""),
                   reexec=calls.append, out=lines.append)
    assert rc == 1 and calls == []
    assert lines == [f"label must be 1 to 31 characters of A-Z, a-z, 0-9, _ and -, got {label!r}"]


@pytest.mark.parametrize("label", ["floficam", "vmtest", "My_Cam-2", "a" * 31])
def test_label_charset_accepts(label):
    lines = []
    assert run_setup(label, find=lambda *a: "/dev/video3", out=lines.append) == 0
