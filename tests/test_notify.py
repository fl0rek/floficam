"""sd_notify STATUS sender. See
docs/superpowers/specs/2026-10-26-ffsb-camera-pause-design.md, "Reporting",
for why the feeder reports STATUS=live/frozen/paused-card over
$NOTIFY_SOCKET and why sends must never block the 30 Hz main loop.
"""
import os
import socket

from floficam.notify import send_status


def test_sends_status_datagram(tmp_path):
    path = str(tmp_path / "notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    assert send_status("frozen", {"NOTIFY_SOCKET": path}) is True
    assert srv.recv(256) == b"STATUS=frozen"


def test_abstract_socket():
    name = f"floficam-test-{os.getpid()}"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0" + name)
    assert send_status("live", {"NOTIFY_SOCKET": "@" + name}) is True
    assert srv.recv(256) == b"STATUS=live"


def test_unset_is_noop():
    assert send_status("live", {}) is False


def test_dead_socket_returns_false(tmp_path):
    assert send_status("live", {"NOTIFY_SOCKET": str(tmp_path / "nope")}) is False


def test_socket_creation_failure_returns_false(tmp_path, monkeypatch):
    """socket.socket() itself can raise (EMFILE/ENOBUFS); send_status's
    never-raises contract must hold even for that, not just for sendto()
    — this runs on the 30 Hz tick path, where a raise would quit the
    feeder."""
    def raising_socket(*args, **kwargs):
        raise OSError("EMFILE")

    monkeypatch.setattr(socket, "socket", raising_socket)
    assert send_status("live", {"NOTIFY_SOCKET": str(tmp_path / "notify")}) is False


def test_sendto_failure_still_closes_the_socket(tmp_path, monkeypatch):
    """A raising sendto() must not leak the already-created socket."""
    closed = []
    real_socket_cls = socket.socket

    class TrackingSocket(real_socket_cls):
        def sendto(self, *args, **kwargs):
            raise OSError("ENOBUFS")

        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: TrackingSocket(*a, **kw))
    assert send_status("live", {"NOTIFY_SOCKET": str(tmp_path / "notify")}) is False
    assert closed == [True]
