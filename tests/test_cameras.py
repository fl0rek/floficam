"""`--camera auto`'s choice over fake PipeWire device lists: only hardware
cameras, never a loopback, never our own output, never a screencast.
The fixtures' property names are the ones PipeWire reports for the Apple
ISP camera on westfall."""
import os

from floficam.cameras import Camera, pick, usable

REAL = {"device.api": "v4l2", "node.name": "v4l2_input.platform-isp", "api.v4l2.cap.driver": "apple-isp",
        "api.v4l2.path": "/dev/video0", "description": "FaceTime HD"}
LOOP = {"device.api": "v4l2", "node.name": "v4l2_input.loop", "api.v4l2.cap.driver": "v4l2 loopback",
        "api.v4l2.path": "/dev/video10", "description": "floficam"}
SCREEN = {"node.name": "xdpw_screencast", "media.class": "Video/Source", "description": "Screen"}


def test_loopbacks_and_output_skipped():
    assert [c.target for c in usable([LOOP, REAL], "/dev/video10")] == ["v4l2_input.platform-isp"]
    other = dict(REAL, **{"api.v4l2.cap.driver": "uvcvideo"})
    assert usable([other], "/dev/video0") == []          # same path as the output


def test_screencast_dropped():
    assert [c.target for c in usable([SCREEN, REAL], None)] == ["v4l2_input.platform-isp"]
    assert usable([SCREEN], None) == []


def test_serial_fallback_and_nameless_dropped():
    assert usable([{"device.api": "v4l2", "node.name": "cam with space", "object.serial": "42"}], None)[0].target == "42"
    assert usable([{"device.api": "v4l2", "node.name": "cam with space"}], None) == []
    assert usable([{"device.api": "v4l2", "description": "?"}], None) == []


def test_pick_none():
    assert pick("/dev/video10", list_devices=lambda: [LOOP, SCREEN]) is None


def test_pick_first_usable_keeps_order():
    second = dict(REAL, **{"node.name": "v4l2_input.usb", "api.v4l2.path": "/dev/video2"})
    assert pick(None, list_devices=lambda: [SCREEN, LOOP, second, REAL]).target == "v4l2_input.usb"


def test_camera_fields():
    assert usable([REAL], None) == [Camera("v4l2_input.platform-isp", "FaceTime HD", "/dev/video0")]


def test_hardware_markers():
    libcam = {"device.api": "libcamera", "node.name": "libcamera_input.a", "description": "A"}
    libcam_key = {"api.libcamera.location": "front", "node.name": "libcamera_input.b", "description": "B"}
    v4l2_path = {"api.v4l2.path": "/dev/video4", "node.name": "v4l2_input.c", "description": "C"}
    other_api = {"device.api": "bluez5", "node.name": "virtual.d", "description": "D"}
    assert [c.target for c in usable([libcam, libcam_key, v4l2_path, other_api], None)] == [
        "libcamera_input.a", "libcamera_input.b", "v4l2_input.c"]


def test_output_matched_through_symlinks(tmp_path):
    """`/dev/floficam` is a symlink to the loopback's /dev/videoN: compared
    by realpath, on either side."""
    real = tmp_path / "video7"
    real.touch()
    os.symlink(real, tmp_path / "floficam")
    cam = dict(REAL, **{"api.v4l2.cap.driver": "uvcvideo", "api.v4l2.path": str(real)})
    assert usable([cam], str(tmp_path / "floficam")) == []
    by_device_path = {"device.api": "v4l2", "node.name": "n", "device.path": str(real), "description": "x"}
    assert usable([by_device_path], str(tmp_path / "floficam")) == []
    assert len(usable([cam], str(tmp_path / "other"))) == 1
