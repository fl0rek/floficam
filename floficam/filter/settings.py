"""Settings, presets and validation for the filter child.

No GStreamer, no numpy, no clock: pure parsing/validation over strings and
dicts, so the effect chain (a later task) can stay thin. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Paths"
and "Settings and presets", for the file formats and validation rules
pinned here.

`load_presets` never raises on a broken *user* presets.json: an
unreadable or unparseable file, or an individual preset with a bad knob,
an id with whitespace or an id that shadows a built-in, is recorded in
`skipped` (preset id -> reason, or the file's own name when nothing in
it could be matched to an id) and left out of the returned presets. A
broken *built-in* presets.json is a packaging bug, not a user mistake,
so it raises (a SettingsError) instead.
"""
from __future__ import annotations

import importlib.resources
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Temporal:
    fps_cap: int = 30
    stalls_per_min: float = 0.0
    stall_len_s: float = 1.0


@dataclass(frozen=True)
class Picture:
    width: int = 1280
    upscale: str = "blocky"
    shutter: float = 0.0
    tint: str = "none"
    colour: str = "true"
    levels: int = 8
    palette: str = "web216"
    dither: str = "none"


@dataclass(frozen=True)
class Codec:
    kind: str = "off"
    jpeg_quality: int = 50
    vp8_kbps: int = 600
    mosh: float = 0.0


@dataclass(frozen=True)
class Analog:
    interlace: bool = False
    chroma_shift: int = 0
    chroma_smear: int = 0
    scanlines: float = 0.0
    grain: float = 0.0


@dataclass(frozen=True)
class Knobs:
    temporal: Temporal = field(default_factory=Temporal)
    picture: Picture = field(default_factory=Picture)
    codec: Codec = field(default_factory=Codec)
    analog: Analog = field(default_factory=Analog)


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    builtin: bool
    knobs: Knobs


@dataclass(frozen=True)
class Resolved:
    preset_id: str
    modified: bool
    knobs: Knobs


class SettingsError(Exception):
    """The reason shown to florek: in STATUS's `error` field, and in the
    journal."""


# --- Validation -------------------------------------------------------
#
# Each validator takes the raw JSON value for one knob and either returns
# it unchanged or raises SettingsError naming the knob (group.knob) and
# the reason. A bool is never a number: Python's bool is an int subclass,
# so every numeric validator rejects it explicitly before the range/
# membership check.


def _choice(name: str, choices: tuple) -> callable:
    rendered = ", ".join(str(c) for c in choices)

    def validate(value):
        if isinstance(value, bool) or value not in choices:
            raise SettingsError(f"{name}: {value!r} not in {{{rendered}}}")
        return value

    return validate


def _range(name: str, lo: float, hi: float, *, integer: bool = False) -> callable:
    kind = "an int" if integer else "a number"

    def validate(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{name}: {value!r} is not {kind}")
        if integer and not isinstance(value, int):
            raise SettingsError(f"{name}: {value!r} is not {kind}")
        if not (lo <= value <= hi):
            raise SettingsError(f"{name}: {value!r} not in [{lo}, {hi}]")
        return value

    return validate


def _bool(name: str) -> callable:
    def validate(value):
        if not isinstance(value, bool):
            raise SettingsError(f"{name}: {value!r} is not a bool")
        return value

    return validate


_GROUPS = {"temporal": Temporal, "picture": Picture, "codec": Codec, "analog": Analog}

_VALIDATORS: dict[str, dict[str, callable]] = {
    "temporal": {
        "fps_cap": _choice("temporal.fps_cap", (30, 15, 10, 5, 2, 1)),
        "stalls_per_min": _range("temporal.stalls_per_min", 0, 20),
        "stall_len_s": _range("temporal.stall_len_s", 0.2, 3),
    },
    "picture": {
        "width": _choice("picture.width", (1280, 640, 320, 256, 160)),
        "upscale": _choice("picture.upscale", ("blocky", "soft")),
        "shutter": _range("picture.shutter", 0, 0.95),
        "tint": _choice("picture.tint", ("none", "mono", "sepia", "cctv")),
        "colour": _choice("picture.colour", ("true", "posterize", "palette")),
        "levels": _range("picture.levels", 2, 64, integer=True),
        "palette": _choice("picture.palette", ("2", "cga", "gameboy", "win16", "web216")),
        "dither": _choice("picture.dither", ("none", "bayer4", "bayer8")),
    },
    "codec": {
        "kind": _choice("codec.kind", ("off", "jpeg", "vp8")),
        "jpeg_quality": _range("codec.jpeg_quality", 1, 100, integer=True),
        "vp8_kbps": _range("codec.vp8_kbps", 16, 2000, integer=True),
        "mosh": _range("codec.mosh", 0, 1),
    },
    "analog": {
        "interlace": _bool("analog.interlace"),
        "chroma_shift": _range("analog.chroma_shift", 0, 16, integer=True),
        "chroma_smear": _range("analog.chroma_smear", 0, 16, integer=True),
        "scanlines": _range("analog.scanlines", 0, 1),
        "grain": _range("analog.grain", 0, 1),
    },
}


def knobs_from_dict(d: Mapping, base: Knobs) -> Knobs:
    """Strict partial merge: `d` is `{group: {knob: value}}`, any subset of
    groups and knobs. Every present value is validated; an unknown group,
    an unknown knob, or an out-of-range/wrong-type value raises
    SettingsError. Knobs absent from `d` keep `base`'s value."""
    if not isinstance(d, Mapping):
        raise SettingsError("knobs: expected an object")
    updates = {}
    for group, group_values in d.items():
        if group not in _GROUPS:
            raise SettingsError(f"{group}: unknown knob group")
        if not isinstance(group_values, Mapping):
            raise SettingsError(f"{group}: expected an object")
        validators = _VALIDATORS[group]
        group_updates = {}
        for knob, value in group_values.items():
            if knob not in validators:
                raise SettingsError(f"{group}.{knob}: unknown knob")
            group_updates[knob] = validators[knob](value)
        updates[group] = replace(getattr(base, group), **group_updates)
    return replace(base, **updates)


# --- Presets ------------------------------------------------------------


def _parse_presets_doc(path: Path) -> list:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise SettingsError(f"{path.name}: unreadable: {e}") from e
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"invalid json: {e}") from e
    version = doc.get("version") if isinstance(doc, Mapping) else None
    if version != 1:
        raise SettingsError(f"unsupported version: {version!r}")
    presets = doc.get("presets")
    if not isinstance(presets, list):
        raise SettingsError("presets: expected a list")
    return presets


def _build_preset(entry, *, builtin: bool) -> Preset:
    if not isinstance(entry, Mapping):
        raise SettingsError("preset: expected an object")
    preset_id = entry.get("id")
    if not isinstance(preset_id, str) or not preset_id:
        raise SettingsError("preset: missing id")
    # StatusText's filter= field is whitespace-delimited: the feeder
    # reports such an id as an error, so the child must not run it.
    if any(c.isspace() for c in preset_id):
        raise SettingsError(f"preset {preset_id!r}: id contains whitespace")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise SettingsError(f"preset {preset_id}: missing name")
    knobs = knobs_from_dict(entry.get("knobs") or {}, Knobs())
    return Preset(id=preset_id, name=name, builtin=builtin, knobs=knobs)


def load_presets(builtin_path: Path, user_path: Path | None) -> tuple[dict[str, Preset], dict[str, str]]:
    """Built-ins first (insertion order preserved, so `list(presets)` keeps
    the file's order), then user presets that neither fail to parse nor
    shadow a built-in id. `skipped` maps a skipped user preset's id to its
    reason; a user file that is not even valid JSON/shape is recorded
    under its own file name instead, since no preset id can be blamed. A
    broken *built-in* file raises -- see module docstring."""
    builtin_entries = _parse_presets_doc(builtin_path)
    presets: dict[str, Preset] = {}
    for entry in builtin_entries:
        preset = _build_preset(entry, builtin=True)
        presets[preset.id] = preset

    skipped: dict[str, str] = {}
    if user_path is not None and user_path.exists():
        try:
            user_entries = _parse_presets_doc(user_path)
        except SettingsError as e:
            skipped[user_path.name] = str(e)
            user_entries = []
        for entry in user_entries:
            entry_id = entry.get("id") if isinstance(entry, Mapping) else None
            key = entry_id if isinstance(entry_id, str) and entry_id else user_path.name
            try:
                preset = _build_preset(entry, builtin=False)
            except SettingsError as e:
                skipped[key] = str(e)
                continue
            if preset.id in presets:
                skipped[preset.id] = f"shadows built-in preset {preset.id!r}"
                continue
            presets[preset.id] = preset
    return presets, skipped


def resolve(filter_text: str | None, presets: Mapping[str, Preset], skipped: Mapping[str, str]) -> Resolved:
    """`filter_text` is `filter.json`'s content, or None when the file is
    missing (the explicit "clean video" default: preset `none`, not
    modified). Raises SettingsError on anything else wrong, including an
    active preset id that `skipped` says is unusable."""
    if filter_text is None:
        return Resolved("none", False, presets["none"].knobs)

    try:
        doc = json.loads(filter_text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"invalid json: {e}") from e
    if not isinstance(doc, Mapping):
        raise SettingsError("invalid json: not an object")

    version = doc.get("version")
    if version != 1:
        raise SettingsError(f"unsupported version: {version!r}")

    preset_id = doc.get("preset", "none")
    if not isinstance(preset_id, str):
        raise SettingsError(f"preset: {preset_id!r} is not a string")
    if preset_id in skipped:
        raise SettingsError(f"preset {preset_id}: {skipped[preset_id]}")
    if preset_id not in presets:
        raise SettingsError(f"unknown preset: {preset_id}")
    preset = presets[preset_id]

    overrides = doc.get("overrides") or {}
    if overrides:
        knobs = knobs_from_dict(overrides, preset.knobs)
        modified = True
    else:
        knobs = preset.knobs
        modified = False
    return Resolved(preset_id, modified, knobs)


# --- Paths ---------------------------------------------------------------


def builtin_presets_path() -> Path:
    """The read-only, package-data built-ins. `FLOFICAM_PRESETS` lets
    tests and the preview helper point elsewhere."""
    override = os.environ.get("FLOFICAM_PRESETS")
    if override:
        return Path(override)
    return importlib.resources.files("floficam.filter") / "presets.json"


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """`filter.json` and the user's `presets.json` live here:
    `$FLOFICAM_STATE_DIR` if set, else `$XDG_STATE_HOME/floficam`, else
    `~/.local/state/floficam`. See the design spec's "Paths"."""
    override = env.get("FLOFICAM_STATE_DIR")
    if override:
        return Path(override)
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "floficam"
    return Path(env["HOME"]) / ".local" / "state" / "floficam"


def preview_dir(env: Mapping[str, str] = os.environ) -> Path:
    """The Control Panel applet's preview socket/frames live here."""
    return Path(env["XDG_RUNTIME_DIR"]) / "floficam-preview"
