"""The V4L2 adapter's ctypes/ioctl constants against the real kernel
headers, and its fd/poll/ioctl seams against fakes (never touches a real
device). See the pre-plan gates' "V4L2 adapter prototype" and Global
Constraints' Control IDs.
"""
import ctypes
import errno
import shutil
import subprocess
import textwrap

import pytest

from floficam import v4l2

PROBE_C = textwrap.dedent("""\
    #include <stdio.h>
    #include <linux/videodev2.h>

    int main(void) {
        printf("%u\\n", (unsigned)VIDIOC_QUERYCAP);
        printf("%u\\n", (unsigned)VIDIOC_S_FMT);
        printf("%u\\n", (unsigned)VIDIOC_REQBUFS);
        printf("%u\\n", (unsigned)VIDIOC_QUERYBUF);
        printf("%u\\n", (unsigned)VIDIOC_QBUF);
        printf("%u\\n", (unsigned)VIDIOC_S_CTRL);
        printf("%u\\n", (unsigned)VIDIOC_SUBSCRIBE_EVENT);
        printf("%u\\n", (unsigned)VIDIOC_DQEVENT);
        printf("%zu\\n", sizeof(struct v4l2_format));
        printf("%zu\\n", sizeof(struct v4l2_event));
        printf("%zu\\n", sizeof(struct v4l2_buffer));
        return 0;
    }
    """)


def test_ioctl_numbers_match_headers(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc")
    assert cc, "no C compiler on PATH"
    src = tmp_path / "probe.c"
    src.write_text(PROBE_C)
    binary = tmp_path / "probe"
    subprocess.run([cc, str(src), "-o", str(binary)], check=True)
    out = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    lines = [int(x) for x in out.split()]
    (
        querycap, s_fmt, reqbufs, querybuf, qbuf, s_ctrl, subscribe_event, dqevent,
        format_size, event_size, buffer_size,
    ) = lines

    assert querycap == v4l2.VIDIOC_QUERYCAP
    assert s_fmt == v4l2.VIDIOC_S_FMT
    assert reqbufs == v4l2.VIDIOC_REQBUFS
    assert querybuf == v4l2.VIDIOC_QUERYBUF
    assert qbuf == v4l2.VIDIOC_QBUF
    assert s_ctrl == v4l2.VIDIOC_S_CTRL
    assert subscribe_event == v4l2.VIDIOC_SUBSCRIBE_EVENT
    assert dqevent == v4l2.VIDIOC_DQEVENT
    assert format_size == ctypes.sizeof(v4l2.Format)
    assert event_size == ctypes.sizeof(v4l2.Event)
    assert buffer_size == ctypes.sizeof(v4l2.Buffer)


def test_control_ids():
    assert v4l2.CID_KEEP_FORMAT == 0x98F900
    assert v4l2.CID_TIMEOUT == 0x98F902
    assert v4l2.CID_TIMEOUT_IMAGE_IO == 0x98F903
    assert v4l2.EVENT_PRI_CLIENT_USAGE == 0x10E00001


def test_read_usage_nonblocking_when_empty(monkeypatch):
    monkeypatch.setattr(v4l2.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(v4l2.os, "close", lambda fd: None)

    class EmptyPoll:
        def register(self, fd, events):
            pass

        def poll(self, timeout_ms):
            return []

    monkeypatch.setattr(v4l2.select, "poll", lambda: EmptyPoll())

    def no_dqevent(fd, req, arg, mutate=True):
        raise AssertionError("DQEVENT must not be called when nothing is pending")

    monkeypatch.setattr(v4l2.fcntl, "ioctl", no_dqevent)

    p = v4l2.Producer("/dev/video10")
    assert p.read_usage() is None


def _fake_ioctl_for_configure(held_format):
    def fake(fd, req, arg, mutate=True):
        if req == v4l2.VIDIOC_S_CTRL:
            return arg
        if req == v4l2.VIDIOC_S_FMT:
            raise OSError(errno.EBUSY, "format held by another opener")
        if req == v4l2.VIDIOC_G_FMT:
            arg.fmt.pix.width, arg.fmt.pix.height, arg.fmt.pix.pixelformat = held_format
            return arg
        raise AssertionError(f"unexpected ioctl {req:#x}")

    return fake


def test_configure_accepts_held_equal_format(monkeypatch):
    monkeypatch.setattr(v4l2.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(v4l2.os, "close", lambda fd: None)

    monkeypatch.setattr(v4l2.fcntl, "ioctl", _fake_ioctl_for_configure((1280, 720, v4l2.NV12)))
    p = v4l2.Producer("/dev/video10")
    p.configure(1280, 720, "NV12")  # no exception: S_FMT's EBUSY, G_FMT confirms the held format

    monkeypatch.setattr(v4l2.fcntl, "ioctl", _fake_ioctl_for_configure((640, 480, v4l2.NV12)))
    p2 = v4l2.Producer("/dev/video10")
    with pytest.raises(v4l2.FormatMismatch):
        p2.configure(1280, 720, "NV12")


class _FakeMmap:
    def __init__(self, size):
        self.data = bytearray(size)

    def __setitem__(self, key, value):
        self.data[key] = value

    def close(self):
        pass


def _fake_timeout_image_device(monkeypatch, granted, length):
    """Fakes the second fd's REQBUFS/QUERYBUF/QBUF and the mmap; returns
    the record of QBUF calls and mmaps."""
    seen = {"qbuf": [], "maps": []}

    def fake_ioctl(fd, req, arg, mutate=True):
        if req == v4l2.VIDIOC_S_CTRL:
            return arg
        if req == v4l2.VIDIOC_REQBUFS:
            arg.count = granted
            return arg
        if req == v4l2.VIDIOC_QUERYBUF:
            arg.length, arg.m.offset = length, 0
            return arg
        if req == v4l2.VIDIOC_QBUF:
            seen["qbuf"].append((arg.index, arg.bytesused))
            return arg
        raise AssertionError(f"unexpected ioctl {req:#x}")

    def fake_mmap(fd, size, flags, prot, offset=0):
        m = _FakeMmap(size)
        seen["maps"].append(m)
        return m

    monkeypatch.setattr(v4l2.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(v4l2.os, "close", lambda fd: None)
    monkeypatch.setattr(v4l2.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(v4l2.mmap, "mmap", fake_mmap)
    return seen


@pytest.mark.parametrize("granted", [1, 2])
def test_install_timeout_image_accepts_any_grant(monkeypatch, granted):
    """With a reader streaming, REQBUFS(1) on the TIMEOUT fd returns the
    stream's used_buffer_count (2); buffer 0 is still the timeout buffer."""
    frame = b"\x10" * 64
    seen = _fake_timeout_image_device(monkeypatch, granted, length=64)

    v4l2.Producer("/dev/video10").install_timeout_image(frame)

    assert seen["qbuf"] == [(0, len(frame))]
    assert bytes(seen["maps"][0].data) == frame


def test_install_timeout_image_rejects_no_grant(monkeypatch):
    _fake_timeout_image_device(monkeypatch, granted=0, length=64)
    with pytest.raises(v4l2.TimeoutImageLost):
        v4l2.Producer("/dev/video10").install_timeout_image(b"\x10" * 64)


def test_install_timeout_image_rejects_short_buffer(monkeypatch):
    _fake_timeout_image_device(monkeypatch, granted=2, length=32)
    with pytest.raises(v4l2.TimeoutImageLost):
        v4l2.Producer("/dev/video10").install_timeout_image(b"\x10" * 64)
