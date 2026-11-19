"""Settings, presets and validation. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Settings
and presets", for the file formats and the fail-closed rules pinned here.
"""
import json
from pathlib import Path

import pytest

from floficam.filter.settings import (
    Knobs,
    SettingsError,
    builtin_presets_path,
    load_presets,
    resolve,
    state_dir,
)

P, _SKIPPED = load_presets(builtin_presets_path(), None)


def test_missing_file_is_none_preset():
    r = resolve(None, P, {})
    assert (r.preset_id, r.modified, r.knobs) == ("none", False, Knobs())


def test_overrides_merge_and_mark_modified():
    r = resolve(
        '{"version":1,"preset":"vhs","overrides":{"codec":{"vp8_kbps":100}}}', P, {}
    )
    assert r.modified
    assert r.knobs.codec.vp8_kbps == 100
    assert r.knobs.picture.width == 640


@pytest.mark.parametrize(
    "text,needle",
    [
        ("{", "json"),
        ('{"version":2,"preset":"none"}', "version"),
        ('{"version":1,"preset":"gone"}', "unknown preset"),
        ('{"version":1,"preset":"none","overrides":{"codec":{"bitrate":1}}}', "unknown knob"),
        ('{"version":1,"preset":"none","overrides":{"picture":{"width":800}}}', "width"),
        ('{"version":1,"preset":"none","overrides":{"analog":{"grain":true}}}', "grain"),
    ],
)
def test_invalid_raises(text, needle):
    with pytest.raises(SettingsError, match=needle):
        resolve(text, P, {})


def test_builtins_valid_and_ordered():
    assert list(P)[:2] == ["none", "1999-webcam"]
    assert all(p.builtin for p in P.values())


def test_shadowing_user_preset_skipped(tmp_path):
    user_path = tmp_path / "presets.json"
    user_path.write_text(json.dumps({
        "version": 1,
        "presets": [{"id": "vhs", "name": "My VHS", "knobs": {}}],
    }))
    presets, skipped = load_presets(builtin_presets_path(), user_path)
    assert presets["vhs"].builtin
    assert "vhs" in skipped["vhs"]


def test_broken_user_presets_do_not_break_builtin(tmp_path):
    user_path = tmp_path / "presets.json"
    user_path.write_text("{")
    presets, skipped = load_presets(builtin_presets_path(), user_path)
    assert user_path.name in skipped
    r = resolve('{"version":1,"preset":"vhs"}', presets, skipped)
    assert r.preset_id == "vhs"


def test_active_skipped_user_preset_raises(tmp_path):
    user_path = tmp_path / "presets.json"
    user_path.write_text(json.dumps({
        "version": 1,
        "presets": [
            {"id": "mine", "name": "Mine", "knobs": {"analog": {"grain": True}}}
        ],
    }))
    presets, skipped = load_presets(builtin_presets_path(), user_path)
    assert "mine" in skipped
    with pytest.raises(SettingsError, match="mine"):
        resolve('{"version":1,"preset":"mine"}', presets, skipped)


def test_state_dir_override():
    assert state_dir({"FLOFICAM_STATE_DIR": "/x/y", "HOME": "/h"}) == Path("/x/y")


def test_state_dir_xdg():
    assert state_dir({"XDG_STATE_HOME": "/s", "HOME": "/h"}) == Path("/s/floficam")


def test_state_dir_home():
    assert state_dir({"HOME": "/h"}) == Path("/h/.local/state/floficam")


def test_builtin_path_is_package_data():
    assert builtin_presets_path().name == "presets.json"
    assert builtin_presets_path().exists()


@pytest.mark.parametrize("kind", ["non-utf8", "directory"])
def test_unreadable_user_presets_skipped(tmp_path, kind):
    """I3: a user presets.json that can't even be read is a skipped file,
    like one that doesn't parse, never an exception out of load_presets."""
    user_path = tmp_path / "presets.json"
    if kind == "directory":
        user_path.mkdir()
    else:
        user_path.write_bytes(b'{"version":1,"presets":[{"id":"\xff"}]}')
    presets, skipped = load_presets(builtin_presets_path(), user_path)
    assert user_path.name in skipped
    assert resolve('{"version":1,"preset":"vhs"}', presets, skipped).preset_id == "vhs"


def test_unreadable_builtin_presets_raise_settings_error(tmp_path):
    builtin = tmp_path / "builtin.json"
    builtin.write_bytes(b"\xff\xfe")
    with pytest.raises(SettingsError, match="builtin.json"):
        load_presets(builtin, None)


@pytest.mark.parametrize("preset", [["vhs"], {"id": "vhs"}, 7])
def test_non_string_preset_raises(preset):
    with pytest.raises(SettingsError, match="preset"):
        resolve(json.dumps({"version": 1, "preset": preset}), P, {})


def test_user_preset_id_with_whitespace_skipped(tmp_path):
    """M3: StatusText's filter= field can't carry whitespace (the feeder
    turns such an id into `error`), so the child must not run one either."""
    user_path = tmp_path / "presets.json"
    user_path.write_text(json.dumps({
        "version": 1,
        "presets": [
            {"id": "my preset", "name": "Mine", "knobs": {}},
            {"id": "mine", "name": "Mine", "knobs": {}},
        ],
    }))
    presets, skipped = load_presets(builtin_presets_path(), user_path)
    assert "my preset" not in presets and "whitespace" in skipped["my preset"]
    assert "mine" in presets
    with pytest.raises(SettingsError, match="my preset"):
        resolve('{"version":1,"preset":"my preset"}', presets, skipped)
