"""The runtime file a running daemon keeps for other floficam commands:
`$XDG_RUNTIME_DIR/floficam/daemon.json`. A stored (pid, start_time) pair
tells a live daemon from a stale file or a reused pid."""
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path


def runtime_path(env: Mapping[str, str] = os.environ) -> Path | None:
    base = env.get("XDG_RUNTIME_DIR")
    return Path(base) / "floficam" / "daemon.json" if base else None


def proc_start_time(pid: int, *, proc: Path = Path("/proc")) -> int | None:
    """Field 22 of /proc/<pid>/stat; comm (field 2) may hold spaces and
    parentheses, so count from after the last `)`."""
    try:
        text = (proc / str(pid) / "stat").read_text()
        return int(text[text.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def write_runtime(path: Path, *, pid: int, start_time: int, device: str, camera: str, status: str,
                  signal_seq: int = 0, pause_refused: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{pid}.tmp")
    tmp.write_text(json.dumps({
        "pid": pid, "start_time": start_time, "device": device, "camera": camera,
        "status": status, "signal_seq": signal_seq, "pause_refused": pause_refused,
    }))
    os.replace(tmp, path)


def _load(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def read_runtime(path: Path | None, *, proc: Path = Path("/proc")) -> dict | None:
    d = _load(path)
    if d is None:
        return None
    pid = d.get("pid")
    if not isinstance(pid, int) or proc_start_time(pid, proc=proc) != d.get("start_time"):
        return None
    return d


def remove_runtime(path: Path | None, *, pid: int | None = None) -> None:
    d = _load(path)
    if d is not None and d.get("pid") == (os.getpid() if pid is None else pid):
        try:
            path.unlink()
        except OSError:
            pass


class RuntimeNotify:
    """The daemon's status sink: keeps the runtime file current and
    forwards to the sd_notify sender. Never raises."""

    def __init__(self, path: Path | None, send: Callable[[str], bool] | None, *,
                 pid: int, start_time: int, device: str, camera: str):
        self.path = path
        self.send = send
        self.pid = pid
        self.start_time = start_time
        self.device = device
        self.camera = camera
        self.status = ""
        self.signal_seq = 0
        self.pause_refused: str | None = None
        self._logged: set[str] = set()

    def _write(self) -> None:
        if self.path is None:
            return
        try:
            write_runtime(self.path, pid=self.pid, start_time=self.start_time, device=self.device,
                          camera=self.camera, status=self.status, signal_seq=self.signal_seq,
                          pause_refused=self.pause_refused)
        except OSError as e:
            msg = f"runtime file: {e}"
            if msg not in self._logged:
                self._logged.add(msg)
                print("floficam-daemon:", msg, file=sys.stderr, flush=True)

    def notify(self, status: str) -> None:
        self.status = status
        self._write()
        if self.send is not None:
            self.send(status)

    def set_camera(self, camera: str) -> None:
        self.camera = camera
        self._write()

    def signal_result(self, refusal: str | None) -> None:
        self.signal_seq += 1
        self.pause_refused = refusal
        self._write()
