"""sd_notify STATUS sender. See
docs/superpowers/specs/2026-10-26-ffsb-camera-pause-design.md, "Reporting",
for why the feeder reports STATUS=live/frozen/paused-card over
$NOTIFY_SOCKET and why sends must never block the 30 Hz main loop.
"""
import os
import socket

from ffsb_camera.notify import send_status


def test_sends_status_datagram(tmp_path):
    path = str(tmp_path / "notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    assert send_status("frozen", {"NOTIFY_SOCKET": path}) is True
    assert srv.recv(256) == b"STATUS=frozen"


def test_abstract_socket():
    name = f"ffsb-test-{os.getpid()}"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0" + name)
    assert send_status("live", {"NOTIFY_SOCKET": "@" + name}) is True
    assert srv.recv(256) == b"STATUS=live"


def test_unset_is_noop():
    assert send_status("live", {}) is False


def test_dead_socket_returns_false(tmp_path):
    assert send_status("live", {"NOTIFY_SOCKET": str(tmp_path / "nope")}) is False
