import json
from pathlib import Path

from floficam.runtime import (
    RuntimeNotify,
    proc_start_time,
    read_runtime,
    remove_runtime,
    runtime_path,
    write_runtime,
)


def _stat(proc, pid, start):
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    # comm holds spaces and ")"; field 22 (starttime) is the 20th after it.
    rest = ["S", "1"] + ["0"] * 17 + [str(start)] + ["0"] * 5
    (d / "stat").write_text(f"{pid} (a) b) " + " ".join(rest) + "\n")


def _write(p, **kw):
    args = dict(pid=123, start_time=777, device="/dev/video3", camera="external",
                status="camera=live filter=vhs")
    args.update(kw)
    write_runtime(p, **args)


def test_proc_start_time(tmp_path):
    _stat(tmp_path, 123, 777)
    assert proc_start_time(123, proc=tmp_path) == 777
    assert proc_start_time(999, proc=tmp_path) is None


def test_round_trip_and_stale(tmp_path):
    proc = tmp_path / "proc"
    _stat(proc, 123, 777)
    p = tmp_path / "x" / "daemon.json"
    _write(p)
    d = read_runtime(p, proc=proc)
    assert d["status"] == "camera=live filter=vhs"
    assert d["signal_seq"] == 0 and d["pause_refused"] is None
    _stat(proc, 123, 778)  # pid reused
    assert read_runtime(p, proc=proc) is None


def test_dead_pid_and_garbage(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    p = tmp_path / "daemon.json"
    _write(p)
    assert read_runtime(p, proc=proc) is None
    p.write_text("{")
    assert read_runtime(p, proc=proc) is None
    assert read_runtime(None, proc=proc) is None
    assert read_runtime(tmp_path / "missing", proc=proc) is None


def test_remove_leaves_other_pids_file(tmp_path):
    p = tmp_path / "daemon.json"
    _write(p, pid=5)
    remove_runtime(p, pid=6)
    assert p.exists()
    remove_runtime(p, pid=5)
    assert not p.exists()
    remove_runtime(p, pid=5)
    remove_runtime(None)


def test_runtime_path_unset():
    assert runtime_path({}) is None
    assert runtime_path({"XDG_RUNTIME_DIR": ""}) is None
    assert runtime_path({"XDG_RUNTIME_DIR": "/run/user/1"}) == Path("/run/user/1/floficam/daemon.json")


def _notify(p, send=None):
    return RuntimeNotify(p, send, pid=1, start_time=2, device="d", camera="c")


def test_notify_writes_then_sends(tmp_path):
    p = tmp_path / "daemon.json"
    seen = []

    def send(s):
        seen.append((s, json.loads(p.read_text())["status"]))
        return True

    _notify(p, send).notify("camera=live filter=none")
    assert seen == [("camera=live filter=none", "camera=live filter=none")]


def test_notify_without_sender_is_silent(tmp_path, capsys):
    p = tmp_path / "daemon.json"
    _notify(p).notify("a")
    assert json.loads(p.read_text())["status"] == "a"
    assert capsys.readouterr().err == ""


def test_notify_without_path_still_sends():
    seen = []
    _notify(None, lambda s: seen.append(s) or True).notify("a")
    assert seen == ["a"]


def test_runtime_write_failure_does_not_raise(tmp_path, capsys):
    n = RuntimeNotify(tmp_path / "f" / "daemon.json", None, pid=1, start_time=1, device="d", camera="c")
    (tmp_path / "f").write_text("a file, not a dir")
    n.notify("a")
    n.notify("b")
    assert capsys.readouterr().err.count("runtime file:") == 1


def test_signal_result_bumps_seq(tmp_path):
    p = tmp_path / "daemon.json"
    n = _notify(p)
    n.notify("a")
    n.signal_result("input_down")
    d = json.loads(p.read_text())
    assert (d["signal_seq"], d["pause_refused"]) == (1, "input_down")
    n.signal_result(None)
    d = json.loads(p.read_text())
    assert (d["signal_seq"], d["pause_refused"], d["status"]) == (2, None, "a")


def test_set_camera(tmp_path):
    p = tmp_path / "daemon.json"
    n = _notify(p)
    n.notify("a")
    n.set_camera("cam2")
    assert json.loads(p.read_text())["camera"] == "cam2"
