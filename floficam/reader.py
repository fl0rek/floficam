#!/usr/bin/env python3
"""A capture reader for the camera lane's v4l2loopback device, the way
libwebrtc (Firefox) reads it: open O_RDWR|O_NONBLOCK, S_FMT, REQBUFS MMAP,
QBUF all, STREAMON, then poll + DQBUF + QBUF. One line per frame on stdout:

    frame <wall-clock s> <sequence> <mean Y> <mean Y of 4 horizontal bands> <crc>

<crc> is the zlib.crc32 of the whole Y plane, as 8 hex digits: equal crcs
mean the same frame, which tells a frozen frame from a moving one.

The wall clock is time.time(), so the driver can compare it against its own
`date +%s.%N`. Stdlib only (ctypes structs, fcntl ioctls, mmap). Ported from
the pre-plan gates' prototype (spec
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"Gate results"); struct sizes are asserted as there.

Run: floficam-reader [--device /dev/video10] [--frames N] [--seconds S]
Stops after N frames or S seconds (whichever first; 0 = no limit), or on
SIGTERM/SIGINT. It closes without STREAMOFF, as a killed Firefox would; the
module reports usage 0 through the implied REQBUFS(0).

floficam-reader --file F prints the same lines (time 0, sequence = frame
index) for a file of raw 1280x720 NV12 frames, so the driver's references are
measured by the same code as the device's frames.
"""
import argparse
import ctypes as C
import errno
import fcntl
import mmap
import os
import select
import signal
import sys
import time
import zlib

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612

BUF_TYPE_CAPTURE = 1
MEMORY_MMAP = 1
CAP_VIDEO_CAPTURE = 0x1
FIELD_NONE = 1
NV12 = int.from_bytes(b"NV12", "little")
WIDTH, HEIGHT = 1280, 720
BANDS = 4
SAMPLE_STEP = 4  # every 4th luma byte: plenty for a mean, cheap at 30 fps


class Capability(C.Structure):
    _fields_ = [("driver", C.c_char * 16), ("card", C.c_char * 32),
                ("bus_info", C.c_char * 32), ("version", C.c_uint32),
                ("capabilities", C.c_uint32), ("device_caps", C.c_uint32),
                ("reserved", C.c_uint32 * 3)]


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


for _s, _n in ((Capability, 104), (Format, 208), (RequestBuffers, 20),
               (Buffer, 88)):
    assert C.sizeof(_s) == _n, (_s.__name__, C.sizeof(_s))


class Stop(Exception):
    pass


def _stop(signum, frame):
    raise Stop


def ioctl(fd, req, arg):
    fcntl.ioctl(fd, req, arg, True)
    return arg


def luma_means(buf, offset=0):
    """(mean, [mean per horizontal band]) of one frame's Y plane."""
    band = WIDTH * HEIGHT // BANDS
    means = []
    for k in range(BANDS):
        s = buf[offset + k * band:offset + (k + 1) * band:SAMPLE_STEP]
        means.append(sum(s) / len(s))
    return sum(means) / BANDS, means


def line(t, seq, buf, offset=0):
    mean, bands = luma_means(buf, offset)
    crc = zlib.crc32(memoryview(buf)[offset:offset + WIDTH * HEIGHT])
    return (f"frame {t:.6f} {seq} {mean:.2f} " + " ".join(f"{x:.2f}" for x in bands)
            + f" {crc:08x}")


def from_file(path) -> int:
    size = WIDTH * HEIGHT * 3 // 2
    with open(path, "rb") as f:
        data = f.read()
    for i in range(len(data) // size):
        print(line(0, i, data, i * size))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="/dev/video10")
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--file")
    args = ap.parse_args(argv)
    if args.file:
        return from_file(args.file)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    fd = os.open(args.device, os.O_RDWR | os.O_NONBLOCK)
    maps = []
    try:
        cap = ioctl(fd, VIDIOC_QUERYCAP, Capability())
        if not cap.device_caps & CAP_VIDEO_CAPTURE:
            print(f"error no capture cap: {cap.device_caps:#x}", flush=True)
            return 1
        f = Format(type=BUF_TYPE_CAPTURE)
        p = f.fmt.pix
        p.width, p.height, p.pixelformat, p.field = WIDTH, HEIGHT, NV12, FIELD_NONE
        p.bytesperline, p.sizeimage = WIDTH, WIDTH * HEIGHT * 3 // 2
        p = ioctl(fd, VIDIOC_S_FMT, f).fmt.pix
        if (p.width, p.height, p.pixelformat) != (WIDTH, HEIGHT, NV12):
            print(f"error format {p.width}x{p.height} {p.pixelformat:#x}", flush=True)
            return 1
        n = ioctl(fd, VIDIOC_REQBUFS, RequestBuffers(
            count=4, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)).count
        for i in range(n):
            b = ioctl(fd, VIDIOC_QUERYBUF, Buffer(index=i, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
            maps.append(mmap.mmap(fd, b.length, mmap.MAP_SHARED,
                                  mmap.PROT_READ | mmap.PROT_WRITE, offset=b.m.offset))
            ioctl(fd, VIDIOC_QBUF, Buffer(index=i, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
        ioctl(fd, VIDIOC_STREAMON, C.c_int(BUF_TYPE_CAPTURE))
        print(f"streaming {time.time():.6f} buffers={n}", flush=True)

        poller = select.poll()
        poller.register(fd, select.POLLIN)
        start = time.monotonic()
        got = 0
        while True:
            if args.frames and got >= args.frames:
                break
            left = args.seconds - (time.monotonic() - start) if args.seconds else 1.0
            if left <= 0:
                break
            if not poller.poll(int(min(left, 1.0) * 1000)):
                continue
            try:
                b = ioctl(fd, VIDIOC_DQBUF, Buffer(type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                raise
            out = line(time.time(), b.sequence, maps[b.index])
            ioctl(fd, VIDIOC_QBUF, Buffer(index=b.index, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
            got += 1
            print(out, flush=True)
    except Stop:
        pass
    finally:
        for m in maps:
            m.close()
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
