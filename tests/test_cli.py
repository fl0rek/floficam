"""The `floficam` command. Pause/resume run against a real child process
standing in for the daemon: its SIGUSR1/SIGUSR2 handlers rewrite the
runtime file the way the daemon does."""
import functools
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import floficam
from floficam import cli, control
from floficam.filter.settings import state_dir

PKG_ROOT = str(Path(floficam.__file__).resolve().parent.parent)

FAKE_DAEMON = textwrap.dedent("""
    import os, signal, subprocess, sys, time
    from floficam.runtime import proc_start_time, runtime_path, write_runtime
    mode = sys.argv[1]
    path = runtime_path()
    me = dict(pid=os.getpid(), start_time=proc_start_time(os.getpid()), device="/dev/video9",
              camera="auto", status="camera=live filter=vhs")
    seq = 0
    def on_sig(signum, frame):
        global seq
        if mode == "silent":
            return
        seq += 1
        refused = mode if (mode != "ok" and signum == signal.SIGUSR1) else None
        write_runtime(path, **me, signal_seq=seq, pause_refused=refused)
    signal.signal(signal.SIGUSR1, on_sig)
    signal.signal(signal.SIGUSR2, on_sig)
    # a process-group sibling with the default SIGUSR1 disposition (dies
    # on it), like the filter child is for the real daemon
    sib = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    write_runtime(path, **me)
    print(sib.pid, flush=True)
    while True:
        signal.pause()
""")


@pytest.fixture
def fake_daemon():
    procs = []

    def start(mode):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([PKG_ROOT, os.environ.get("PYTHONPATH", "")]))
        p = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON, mode], stdout=subprocess.PIPE, text=True,
                             env=env, start_new_session=True)
        procs.append(p)
        sib = int(p.stdout.readline())
        return p, sib

    yield start
    for p in procs:
        try:
            os.killpg(p.pid, 9)
        except ProcessLookupError:
            pass
        p.wait()


def _alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def _out(capsys):
    c = capsys.readouterr()
    return c.out, c.err


# --- overview, help -------------------------------------------------------

def test_no_args_prints_overview(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for cmd in ("setup", "run", "cameras", "status", "preset", "set", "reset", "knobs", "pause", "resume"):
        assert f"floficam {cmd}" in out
    assert cli.main(["-h"]) == 0
    assert capsys.readouterr().out == out


@pytest.mark.parametrize("cmd", ["setup", "run", "cameras", "status", "preset", "set", "reset", "knobs",
                                 "pause", "resume"])
def test_every_command_has_help(cmd, capsys):
    assert cli.main([cmd, "-h"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: floficam {cmd}")
    for word in ("R12", "lane", "Task", "docs/", "spec"):
        assert word not in out
    if cmd == "setup":
        assert "--label" not in out


def test_unknown_command_exits_1(capsys):
    assert cli.main(["bogus"]) == 1
    assert "bogus" in capsys.readouterr().err


# --- settings --------------------------------------------------------------

def test_set_refused_exit_1_and_file_untouched(capsys):
    assert cli.main(["preset", "vhs"]) == 0
    before = (state_dir() / "filter.json").read_bytes()
    capsys.readouterr()
    assert cli.main(["set", "picture.width=320px"]) == 1
    out, err = _out(capsys)
    assert out == "" and "picture.width" in err and "floficam knobs" in err
    assert cli.main(["set", "width=320"]) == 1
    assert "GROUP.KNOB=VALUE" in capsys.readouterr().err
    assert (state_dir() / "filter.json").read_bytes() == before


def test_preset_set_reset_echo(capsys):
    assert cli.main(["preset", "vhs"]) == 0
    out, _ = _out(capsys)
    assert "vhs" in out
    assert cli.main(["set", "picture.width=320", "analog.grain=0.4"]) == 0
    out, _ = _out(capsys)
    assert "vhs" in out and "picture.width=320" in out and "analog.grain=0.4" in out
    assert cli.main(["reset"]) == 0
    out, _ = _out(capsys)
    assert "vhs" in out and "picture.width" not in out
    assert cli.main(["preset", "nope"]) == 1
    assert "unknown preset: nope" in capsys.readouterr().err


def test_preset_list_marks_active_and_user(capsys):
    sd = state_dir()
    sd.mkdir(parents=True)
    (sd / "presets.json").write_text(json.dumps(
        {"version": 1, "presets": [{"id": "mine", "name": "My look", "knobs": {}}]}))
    assert cli.main(["preset", "vhs"]) == 0
    capsys.readouterr()
    assert cli.main(["preset"]) == 0
    rows = {line[2:].split()[0]: line for line in capsys.readouterr().out.splitlines()
            if line.startswith(("  ", "* "))}
    assert list(rows)[0] == "none" and list(rows)[-1] == "mine"
    assert rows["vhs"].startswith("* ") and rows["none"].startswith("  ")
    assert "My look" in rows["mine"] and "your preset" in rows["mine"]


def test_knobs_table(capsys):
    assert cli.main(["set", "picture.width=320"]) == 0
    capsys.readouterr()
    assert cli.main(["knobs"]) == 0
    out = capsys.readouterr().out
    width = next(line for line in out.splitlines() if line.startswith("picture.width"))
    assert "1280, 640, 320, 256, 160" in width and "1280" in width and "320" in width
    interlace = next(line for line in out.splitlines() if line.startswith("analog.interlace"))
    assert "true, false" in interlace


# --- status ------------------------------------------------------------------

def test_status_without_runtime_dir(monkeypatch, capsys):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "can't tell" in out and "XDG_RUNTIME_DIR" in out


def test_status_not_running(capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "not running" in out and "preset" in out


def test_status_running(fake_daemon, capsys):
    p, _ = fake_daemon("ok")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"running (pid {p.pid})" in out and "/dev/video9" in out and "filter=vhs" in out


def test_status_shows_settings_error_and_skipped(capsys):
    sd = state_dir()
    sd.mkdir(parents=True)
    (sd / "filter.json").write_text(json.dumps({"version": 1, "preset": "ghost"}))
    (sd / "presets.json").write_text(json.dumps({"version": 1, "presets": [
        {"id": "bad", "name": "Bad", "knobs": {"picture": {"width": 7}}},
        {"id": "good", "name": "Good", "knobs": {}},
    ]}))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "settings error: unknown preset: ghost" in out
    assert "skipped preset bad: picture.width: 7 not in" in out
    assert "skipped preset good" not in out


# --- run -------------------------------------------------------------------------

def _fake_daemon_module(monkeypatch, got):
    import types
    fake = types.ModuleType("floficam.main")
    fake.main = lambda argv: got.append(argv) or 0
    monkeypatch.setitem(sys.modules, "floficam.main", fake)


def test_run_refuses_when_live(monkeypatch, capsys):
    got = []
    _fake_daemon_module(monkeypatch, got)
    monkeypatch.setattr(control, "daemon", lambda: {"pid": 4242, "device": "/dev/video9"})
    assert cli.main(["run"]) == 1
    err = capsys.readouterr().err
    assert "already running (pid 4242)" in err and "systemctl --user stop floficam" in err
    assert got == []


def test_run_passes_flags_with_auto_default(monkeypatch):
    got = []
    _fake_daemon_module(monkeypatch, got)
    monkeypatch.setattr(control, "daemon", lambda: None)
    assert cli.main(["run"]) == 0
    assert cli.main(["run", "--device", "/dev/video7", "--camera", "external",
                     "--stream-property", "a=b", "--stream-property", "c=d"]) == 0
    assert got[0] == ["--camera", "auto"]
    assert got[1] == ["--camera", "external", "--device", "/dev/video7",
                      "--stream-property", "a=b", "--stream-property", "c=d"]


# --- pause / resume ----------------------------------------------------------

def test_pause_signals_only_the_daemon_pid(fake_daemon, capsys):
    p, sib = fake_daemon("ok")
    assert cli.main(["pause"]) == 0
    assert capsys.readouterr().out.strip() == f"paused (pid {p.pid})"
    assert cli.main(["resume"]) == 0
    assert capsys.readouterr().out.strip() == f"resumed (pid {p.pid})"
    assert _alive(sib), "the daemon's process-group sibling got the signal too"


@pytest.mark.parametrize("refusal,text,code", [
    ("input_down", "pause refused: the camera isn't in use (pause works while something is streaming)", 1),
    ("filter_not_ready", "pause refused: effects not ready yet", 1),
    ("no_fresh_frame", "pause refused: no recent camera frame", 1),
    ("already_frozen", "already paused", 0),
])
def test_pause_refused(fake_daemon, capsys, refusal, text, code):
    fake_daemon(refusal)
    assert cli.main(["pause"]) == code
    out, err = _out(capsys)
    assert (err if code else out).strip() == text


def test_pause_no_answer_exits_1(fake_daemon, monkeypatch, capsys):
    p, _ = fake_daemon("silent")
    monkeypatch.setattr(control, "signal", functools.partial(control.signal, timeout=0.2))
    assert cli.main(["pause"]) == 1
    assert capsys.readouterr().err.strip() == f"signal sent to pid {p.pid} but the daemon didn't confirm"
    assert cli.main(["resume"]) == 1
    assert capsys.readouterr().err.strip() == f"signal sent to pid {p.pid} but the daemon didn't confirm"


def test_pause_not_running(capsys):
    assert cli.main(["pause"]) == 1
    assert "floficam is not running" in capsys.readouterr().err
    assert cli.main(["resume"]) == 1
    assert "floficam is not running" in capsys.readouterr().err


# --- setup and the no-GI promise -------------------------------------------------

def test_setup_passes_label(monkeypatch):
    got = []
    monkeypatch.setattr(cli, "run_setup", lambda label: got.append(label) or 0)
    assert cli.main(["setup"]) == 0
    assert cli.main(["setup", "--label", "vmtest"]) == 0
    assert got == ["floficam", "vmtest"]


NO_GI = textwrap.dedent("""
    import sys
    from floficam import cli, setup
    real = cli.run_setup
    cli.run_setup = lambda label: real(label, find=lambda *a: "/dev/video9", out=print)
    for argv in ([], ["setup"], ["preset"], ["preset", "vhs"], ["set", "picture.width=320"], ["reset"],
                 ["knobs"], ["status"], ["pause"], ["resume"], ["set", "picture.width=7"]):
        cli.main(argv)
    bad = sorted(m for m in ("gi", "floficam.main", "floficam.cameras", "floficam.pwinput")
                 if m in sys.modules)
    print("LOADED", bad)
""")


def test_cli_paths_without_gi(tmp_path):
    """`floficam setup` re-runs itself under sudo through the unwrapped
    script (no GStreamer/GI environment), so everything but `run` and
    `cameras` must work without importing gi."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([PKG_ROOT, os.environ.get("PYTHONPATH", "")]),
               FLOFICAM_STATE_DIR=str(tmp_path / "state"), XDG_RUNTIME_DIR=str(tmp_path / "run"))
    r = subprocess.run([sys.executable, "-c", NO_GI], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "LOADED []" in r.stdout, r.stdout
    assert "floficam device already exists: /dev/video9" in r.stdout
