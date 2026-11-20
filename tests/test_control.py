"""`floficam.control`: filter.json writes (validated first, atomic) and
signalling the daemon named in the runtime file."""
import json
import os
import signal

import pytest

from floficam import control
from floficam.control import (
    DaemonNoAnswer,
    DaemonNotRunning,
    parse_assignment,
    read_filter,
    reset,
    select_preset,
    set_knobs,
    write_filter,
)
from floficam.filter.settings import SettingsError
from floficam.runtime import proc_start_time, runtime_path, write_runtime


def test_read_filter_missing_is_none(tmp_path):
    assert read_filter(tmp_path / "nope") == {"version": 1, "preset": "none"}


def test_write_filter_atomic_and_creates_dir(tmp_path):
    state = tmp_path / "a" / "b"
    write_filter(state, {"version": 1, "preset": "vhs"})
    assert json.loads((state / "filter.json").read_text()) == {"version": 1, "preset": "vhs"}
    assert [p.name for p in state.iterdir()] == ["filter.json"]


@pytest.mark.parametrize("text,want", [
    ("picture.width=320", ("picture", "width", "320")),
    ("codec.kind=jpeg", ("codec", "kind", "jpeg")),
    ("analog.grain=", ("analog", "grain", "")),
    ("picture.tint=a=b", ("picture", "tint", "a=b")),
])
def test_parse_assignment(text, want):
    assert parse_assignment(text) == want


@pytest.mark.parametrize("text", ["width=320", "a.b.c=1", "picture.width", ".width=1", "picture.=1", "=1"])
def test_parse_assignment_refuses(text):
    with pytest.raises(ValueError, match=r"expected GROUP\.KNOB=VALUE, e\.g\. picture\.width=320"):
        parse_assignment(text)


def test_set_refuses_wrong_types_and_leaves_file(tmp_path):
    write_filter(tmp_path, {"version": 1, "preset": "vhs"})
    before = (tmp_path / "filter.json").read_bytes()
    for bad in ("picture.width=320px", "analog.interlace=1", "codec.kind=JPEG", "nope.knob=1", "width=320",
                "picture.nope=1", "analog.grain=2", "picture.width=320.0", "temporal.fps_cap=15.0",
                "temporal.fps_cap=true", "analog.chroma_shift=true"):
        with pytest.raises((SettingsError, ValueError)):
            set_knobs(tmp_path, [bad])
    # one bad assignment among good ones: nothing is written
    with pytest.raises(SettingsError):
        set_knobs(tmp_path, ["picture.width=320", "codec.kind=JPEG"])
    assert (tmp_path / "filter.json").read_bytes() == before


def test_set_refusal_names_the_validator_reason(tmp_path):
    with pytest.raises(SettingsError, match=r"picture\.width: '320px' not in"):
        set_knobs(tmp_path, ["picture.width=320px"])
    with pytest.raises(SettingsError, match=r"analog\.interlace: 1 is not a bool"):
        set_knobs(tmp_path, ["analog.interlace=1"])
    assert not (tmp_path / "filter.json").exists()


def test_set_string_choices_that_look_like_json(tmp_path):
    set_knobs(tmp_path, ["picture.colour=true", "picture.palette=2"])
    assert read_filter(tmp_path)["overrides"]["picture"] == {"colour": "true", "palette": "2"}
    set_knobs(tmp_path, ["analog.interlace=true", "picture.width=320"])
    f = read_filter(tmp_path)["overrides"]
    assert f["analog"]["interlace"] is True and f["picture"]["width"] == 320


def test_set_merges_and_preset_clears(tmp_path):
    set_knobs(tmp_path, ["analog.grain=0.4", "picture.width=320"])
    assert read_filter(tmp_path)["overrides"] == {"analog": {"grain": 0.4}, "picture": {"width": 320}}
    select_preset(tmp_path, "vhs")
    assert read_filter(tmp_path) == {"version": 1, "preset": "vhs"}
    with pytest.raises(SettingsError):
        select_preset(tmp_path, "nope")
    assert read_filter(tmp_path) == {"version": 1, "preset": "vhs"}


def test_set_keeps_preset_and_reset_drops_overrides(tmp_path):
    select_preset(tmp_path, "vhs")
    doc = set_knobs(tmp_path, ["codec.kind=jpeg"])
    assert doc == {"version": 1, "preset": "vhs", "overrides": {"codec": {"kind": "jpeg"}}}
    assert reset(tmp_path) == {"version": 1, "preset": "vhs"}
    assert read_filter(tmp_path) == {"version": 1, "preset": "vhs"}


def test_user_preset_selectable(tmp_path):
    (tmp_path / "presets.json").write_text(json.dumps(
        {"version": 1, "presets": [{"id": "mine", "name": "Mine", "knobs": {"picture": {"width": 320}}}]}))
    assert select_preset(tmp_path, "mine") == {"version": 1, "preset": "mine"}


def test_signal_without_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(DaemonNotRunning, match="XDG_RUNTIME_DIR"):
        control.signal(signal.SIGUSR1)


def test_signal_not_running():
    with pytest.raises(DaemonNotRunning, match="floficam is not running"):
        control.signal(signal.SIGUSR1)


def test_signal_stale_file_sends_nothing(monkeypatch):
    """A runtime file naming a live pid with the wrong start_time (a
    reused pid) is not a daemon: nothing is signalled."""
    write_runtime(runtime_path(), pid=os.getpid(), start_time=1, device="/dev/video9",
                  camera="auto", status="x")
    sent = []
    monkeypatch.setattr(control.os, "kill", lambda *a: sent.append(a))
    with pytest.raises(DaemonNotRunning):
        control.signal(signal.SIGUSR1)
    assert sent == []


def test_signal_waits_for_seq(monkeypatch):
    path = runtime_path()
    me = dict(pid=os.getpid(), start_time=proc_start_time(os.getpid()), device="/dev/video9",
              camera="auto", status="x")
    write_runtime(path, **me, signal_seq=3)
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        write_runtime(path, **me, signal_seq=4, pause_refused="input_down")

    monkeypatch.setattr(control.os, "kill", fake_kill)
    assert control.signal(signal.SIGUSR1, timeout=1) == (os.getpid(), "input_down")
    assert sent == [(os.getpid(), signal.SIGUSR1)]

    monkeypatch.setattr(control.os, "kill", lambda pid, sig: None)
    with pytest.raises(DaemonNoAnswer) as e:
        control.signal(signal.SIGUSR2, timeout=0.1, poll=0.02)
    assert e.value.pid == os.getpid()
