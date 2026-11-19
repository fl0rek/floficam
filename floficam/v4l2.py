"""V4L2 adapter for the v4l2loopback camera lane's OUTPUT side.

Ported from the pre-plan gates' prototype (ctypes mirrors of the uapi
structs; ioctl numbers checked against <linux/videodev2.h> on aarch64 —
tests/test_v4l2_abi.py re-checks them against the real headers at test
time). Trimmed to the feeder's `Producer` interface:
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Start-up and Running.
"""
import ctypes as C
import errno
import fcntl
import mmap
import os
import select

# --- ioctl numbers (aarch64, verified with gcc; see test_v4l2_abi.py) ---
VIDIOC_QUERYCAP = 0x80685600
VIDIOC_G_FMT = 0xC0D05604
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_SUBSCRIBE_EVENT = 0x4020565A
VIDIOC_DQEVENT = 0x80885659

BUF_TYPE_OUTPUT = 2
MEMORY_MMAP = 1
FIELD_NONE = 1
NV12 = int.from_bytes(b"NV12", "little")

# v4l2loopback private controls: V4L2_CID_USER_BASE | 0xf000 + n. Global
# Constraints pins the three the feeder touches.
CID_KEEP_FORMAT = 0x0098F900
CID_TIMEOUT = 0x0098F902
CID_TIMEOUT_IMAGE_IO = 0x0098F903

# V4L2_EVENT_PRIVATE_START + 0x08E00000 + 1.
EVENT_PRI_CLIENT_USAGE = 0x08000000 + 0x08E00000 + 1
EVENT_SUB_FL_SEND_INITIAL = 1


class PixFormat(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in (
        "width", "height", "pixelformat", "field", "bytesperline",
        "sizeimage", "colorspace", "priv", "flags", "ycbcr_enc",
        "quantization", "xfer_func")]


class _FmtUnion(C.Union):
    _fields_ = [("pix", PixFormat), ("raw_data", C.c_uint8 * 200),
                ("_align", C.c_uint64)]


class Format(C.Structure):
    _fields_ = [("type", C.c_uint32), ("fmt", _FmtUnion)]


class RequestBuffers(C.Structure):
    _fields_ = [("count", C.c_uint32), ("type", C.c_uint32),
                ("memory", C.c_uint32), ("capabilities", C.c_uint32),
                ("flags", C.c_uint8), ("reserved", C.c_uint8 * 3)]


class Timeval(C.Structure):
    _fields_ = [("tv_sec", C.c_long), ("tv_usec", C.c_long)]


class Timecode(C.Structure):
    _fields_ = [("type", C.c_uint32), ("flags", C.c_uint32),
                ("frames", C.c_uint8), ("seconds", C.c_uint8),
                ("minutes", C.c_uint8), ("hours", C.c_uint8),
                ("userbits", C.c_uint8 * 4)]


class _BufM(C.Union):
    _fields_ = [("offset", C.c_uint32), ("userptr", C.c_ulong),
                ("planes", C.c_void_p), ("fd", C.c_int32)]


class Buffer(C.Structure):
    _fields_ = [("index", C.c_uint32), ("type", C.c_uint32),
                ("bytesused", C.c_uint32), ("flags", C.c_uint32),
                ("field", C.c_uint32), ("timestamp", Timeval),
                ("timecode", Timecode), ("sequence", C.c_uint32),
                ("memory", C.c_uint32), ("m", _BufM), ("length", C.c_uint32),
                ("reserved2", C.c_uint32), ("request_fd", C.c_int32)]


class Control(C.Structure):
    _fields_ = [("id", C.c_uint32), ("value", C.c_int32)]


class Timespec(C.Structure):
    _fields_ = [("tv_sec", C.c_long), ("tv_nsec", C.c_long)]


class _EvU(C.Union):
    _fields_ = [("data", C.c_uint8 * 64), ("_align", C.c_uint64)]


class Event(C.Structure):
    _fields_ = [("type", C.c_uint32), ("u", _EvU), ("pending", C.c_uint32),
                ("sequence", C.c_uint32), ("timestamp", Timespec),
                ("id", C.c_uint32), ("reserved", C.c_uint32 * 8)]


class EventSubscription(C.Structure):
    _fields_ = [("type", C.c_uint32), ("id", C.c_uint32),
                ("flags", C.c_uint32), ("reserved", C.c_uint32 * 5)]


class FormatMismatch(Exception):
    """S_FMT (or the G_FMT fallback) did not return the requested format:
    another opener holds the OUTPUT format."""

    def __init__(self, got, want):
        self.got, self.want = got, want
        super().__init__(f"format held by another opener: got {got}, want {want}")


class TimeoutImageLost(Exception):
    """The second fd meant to receive the TIMEOUT I/O token did not: a
    foreign opener raced it, or the buffer it got is the wrong size."""


def _fourcc(fourcc: str) -> int:
    return int.from_bytes(fourcc.encode("ascii"), "little")


def _pix_fmt(width, height, fourcc_int):
    f = Format(type=BUF_TYPE_OUTPUT)
    p = f.fmt.pix
    p.width, p.height, p.pixelformat, p.field = width, height, fourcc_int, FIELD_NONE
    # The feeder only ever asks for NV12 (Global Constraints' Format);
    # bytesperline/sizeimage have no general formula, so a different
    # fourcc here is a caller bug, not a format the loopback should see.
    if fourcc_int != NV12:
        raise ValueError(f"only NV12 is supported, got fourcc {fourcc_int:#x}")
    p.bytesperline, p.sizeimage = width, width * height * 3 // 2
    return f


class Producer:
    """The feeder's one opener of the loopback device's OUTPUT side."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)

    def _ioctl(self, req, arg):
        fcntl.ioctl(self.fd, req, arg, True)
        return arg

    def configure(self, width: int, height: int, fourcc: str) -> None:
        """Start-up order (gate 2): unlock, S_FMT, verify, lock.
        `keep_format=1` freezes whatever format is current, so it must be
        unset before S_FMT or it silently locks the default format in
        place; S_FMT is reasserted on every feeder restart for the same
        reason.

        A returned format equal to the request is success however S_FMT
        got there: either it returned the format directly, or — when a
        capture reader is already active and the module keeps the held
        format — it raised EBUSY and G_FMT shows what's held.
        """
        self._ioctl(VIDIOC_S_CTRL, Control(CID_KEEP_FORMAT, 0))
        fourcc_int = _fourcc(fourcc)
        try:
            fmt = self._ioctl(VIDIOC_S_FMT, _pix_fmt(width, height, fourcc_int))
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise
            fmt = self._ioctl(VIDIOC_G_FMT, Format(type=BUF_TYPE_OUTPUT))

        got = (fmt.fmt.pix.width, fmt.fmt.pix.height, fmt.fmt.pix.pixelformat)
        want = (width, height, fourcc_int)
        if got != want:
            raise FormatMismatch(got, want)

        self._ioctl(VIDIOC_S_CTRL, Control(CID_KEEP_FORMAT, 1))

    def set_timeout(self, ms: int) -> None:
        self._ioctl(VIDIOC_S_CTRL, Control(CID_TIMEOUT, ms))

    def install_timeout_image(self, frame: bytes) -> None:
        """No root needed (gate 4): arm `timeout_image_io` on this fd,
        then at once open a second fd, which the module hands the
        TIMEOUT I/O method. The module gives the role to whoever opens
        next, so a foreign opener in that window gets it instead — this
        raises TimeoutImageLost rather than silently feeding a short or
        garbage image."""
        self._ioctl(VIDIOC_S_CTRL, Control(CID_TIMEOUT_IMAGE_IO, 1))
        fd = os.open(self.path, os.O_RDWR)
        try:
            rb = RequestBuffers(count=1, type=BUF_TYPE_OUTPUT, memory=MEMORY_MMAP)
            try:
                fcntl.ioctl(fd, VIDIOC_REQBUFS, rb, True)
            except OSError as e:
                raise TimeoutImageLost(f"REQBUFS failed: {e}") from e
            # Not `!= 1`: while other openers hold buffers (a reader
            # streaming across a feeder restart) the module grants the
            # stream's used_buffer_count, though the opener still got the
            # TIMEOUT role (v4l2loopback.c vidioc_reqbufs). Buffer 0 is the
            # timeout buffer either way. The length check below only
            # catches a buffer too small for the bars; it cannot tell that
            # a foreign opener stole the role. That costs at most what the
            # sandbox sees while the feeder is dead or stalled (the module
            # serves the timeout image only once no frame has been written
            # for `timeout` ms), and never a real frame.
            if rb.count < 1:
                raise TimeoutImageLost(f"REQBUFS granted {rb.count} buffers, want at least 1")

            buf = Buffer(index=0, type=BUF_TYPE_OUTPUT, memory=MEMORY_MMAP)
            fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf, True)
            if buf.length < len(frame):
                raise TimeoutImageLost(f"timeout buffer is {buf.length} bytes, frame is {len(frame)}")

            m = mmap.mmap(fd, buf.length, mmap.MAP_SHARED,
                          mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.m.offset)
            try:
                m[:len(frame)] = frame
            finally:
                m.close()

            fcntl.ioctl(fd, VIDIOC_QBUF, Buffer(
                index=0, type=BUF_TYPE_OUTPUT, memory=MEMORY_MMAP,
                bytesused=len(frame)), True)
        finally:
            os.close(fd)

    def write(self, frame: bytes) -> int:
        return os.write(self.fd, frame)

    def subscribe_usage(self) -> None:
        self._ioctl(VIDIOC_SUBSCRIBE_EVENT, EventSubscription(
            type=EVENT_PRI_CLIENT_USAGE, flags=EVENT_SUB_FL_SEND_INITIAL))

    def read_usage(self) -> int | None:
        """The fd is blocking, so poll first (POLLPRI, timeout 0): a
        DQEVENT with nothing pending would hang the main loop."""
        p = select.poll()
        p.register(self.fd, select.POLLPRI)
        if not p.poll(0):
            return None
        ev = self._ioctl(VIDIOC_DQEVENT, Event())
        return int.from_bytes(bytes(ev.u.data[:4]), "little")

    def fileno(self) -> int:
        return self.fd

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
