"""sd_notify STATUS sender. See
docs/superpowers/specs/2026-10-26-ffsb-camera-pause-design.md, "Reporting",
for why the feeder reports STATUS=live/frozen/paused-card over
$NOTIFY_SOCKET, and why a send must never block the 30 Hz main loop.
"""
import os
import socket
from collections.abc import Mapping


def send_status(status: str, environ: Mapping[str, str] = os.environ) -> bool:
    """Send one `STATUS=<status>` datagram to `environ["NOTIFY_SOCKET"]`.

    A leading `@` in the address names the Linux abstract namespace
    (mapped here to a leading NUL, same as sd_notify(3)). Sent with
    MSG_DONTWAIT so a full queue never stalls the caller. Returns False
    without raising when the variable is unset or on any OSError
    (BlockingIOError included, and socket() itself failing with EMFILE/
    ENOBUFS — this runs on the 30 Hz tick path, so a raise here would quit
    the feeder); True once the datagram is handed off.
    """
    addr = environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.sendto(b"STATUS=" + status.encode(), socket.MSG_DONTWAIT, addr)
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()
    return True
