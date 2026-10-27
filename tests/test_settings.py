"""Settings, presets and validation. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Settings
and presets", for the file formats and the fail-closed rules pinned here.
"""
import json
from pathlib import Path

import pytest

from ffsb_camera.filter.settings import (
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


def test_state_dir_fallback():
    assert state_dir({"HOME": "/h"}) == Path("/h/.local/state/ffsb-camera")


def test_builtin_path_is_package_data():
    assert builtin_presets_path().name == "presets.json"
    assert builtin_presets_path().exists()
