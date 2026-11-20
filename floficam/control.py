"""What the `floficam` command does, without the printing: filter.json
writes (validated the way the filter child validates, then written
atomically, so a typo never reaches the camera) and signalling the daemon
named in the runtime file. No GStreamer here: `floficam setup` re-runs
under sudo without the GI environment, and so may every command but
`run` and `cameras`."""
import copy
import json
import os
import tempfile
import time
from pathlib import Path

from floficam.filter.settings import (
    Resolved,
    SettingsError,
    builtin_presets_path,
    knob_validators,
    load_presets,
    resolve,
)
from floficam.runtime import read_runtime, runtime_path

FILTER = "filter.json"
USER_PRESETS = "presets.json"
ASSIGNMENT_HINT = "expected GROUP.KNOB=VALUE, e.g. picture.width=320"


class BrokenFilter(SettingsError):
    """filter.json itself is what's wrong, not what was asked for."""


class DaemonNotRunning(Exception):
    pass


class DaemonNoAnswer(Exception):
    def __init__(self, pid: int):
        super().__init__(f"signal sent to pid {pid} but the daemon didn't confirm")
        self.pid = pid


def presets(state: Path):
    """`(presets, skipped)`: the built-ins, then the user's presets.json in
    `state`. See `settings.load_presets`."""
    return load_presets(builtin_presets_path(), state / USER_PRESETS)


def read_filter(state: Path) -> dict:
    """filter.json as a dict; a missing file is the `none` preset. Raises
    BrokenFilter when the file can't be read or isn't a JSON object."""
    path = state / FILTER
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {"version": 1, "preset": "none"}
    except (OSError, UnicodeDecodeError) as e:
        raise BrokenFilter(f"{FILTER}: unreadable: {e}") from e
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise BrokenFilter(f"{FILTER}: invalid json: {e}") from e
    if not isinstance(doc, dict):
        raise BrokenFilter(f"{FILTER}: not a JSON object")
    return doc


def write_filter(state: Path, doc: dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state, prefix=f".{FILTER}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, state / FILTER)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validated_write(state: Path, doc: dict, *, blame_file: bool = False) -> dict:
    """`blame_file`: `doc` was built on the current filter.json, so when
    it is refused and the file on its own is refused too, the file is
    what's broken (BrokenFilter), not the change asked for."""
    found, skipped = presets(state)
    try:
        resolve(json.dumps(doc), found, skipped)
    except SettingsError as e:
        if blame_file:
            _, current_error, _ = effective(state)
            if current_error is not None:
                raise BrokenFilter(f"{FILTER}: {current_error}") from e
        raise
    write_filter(state, doc)
    return doc


def parse_assignment(text: str) -> tuple[str, str, str]:
    key, eq, value = text.partition("=")
    group, dot, knob = key.partition(".")
    if not eq or not dot or not group or not knob or "." in knob:
        raise ValueError(ASSIGNMENT_HINT)
    return group, knob, value


def _coerce(group: str, knob: str, raw: str):
    """The raw string if the knob takes it, else its JSON reading (so
    `palette=2` stays the string "2" and `width=320` becomes 320)."""
    validators = knob_validators()
    if group not in validators:
        raise SettingsError(f"{group}: unknown knob group")
    if knob not in validators[group]:
        raise SettingsError(f"{group}.{knob}: unknown knob")
    validate = validators[group][knob]
    try:
        return validate(raw)
    except SettingsError as raw_error:
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise raw_error from None
    return validate(parsed)


def set_knobs(state: Path, assignments: list[str]) -> dict:
    """Merge `group.knob=value` assignments into filter.json's overrides.
    Nothing is written unless every assignment and the resulting file are
    valid."""
    parsed = [parse_assignment(a) for a in assignments]
    doc = copy.deepcopy(read_filter(state))
    overrides = doc.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise BrokenFilter(f"{FILTER}: overrides: expected an object")
    for group, knob, raw in parsed:
        value = _coerce(group, knob, raw)
        group_doc = overrides.setdefault(group, {})
        if not isinstance(group_doc, dict):
            raise BrokenFilter(f"{FILTER}: overrides.{group}: expected an object")
        group_doc[knob] = value
    doc["overrides"] = overrides
    return _validated_write(state, doc, blame_file=True)


def select_preset(state: Path, preset_id: str) -> dict:
    """Make `preset_id` active, dropping any overrides."""
    return _validated_write(state, {"version": 1, "preset": preset_id})


def reset(state: Path) -> dict:
    """Drop the overrides, keeping the active preset."""
    return _validated_write(state, {"version": 1, "preset": read_filter(state).get("preset", "none")},
                            blame_file=True)


def effective(state: Path) -> tuple[Resolved | None, str | None, dict[str, str]]:
    """What the filter child would make of `state` right now:
    `(resolved, error, skipped)`. Never raises: `error` is the message the
    child would report, `skipped` the user presets it would leave out."""
    try:
        found, skipped = presets(state)
    except SettingsError as e:
        return None, str(e), {}
    try:
        text = (state / FILTER).read_text()
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError) as e:
        return None, f"{FILTER}: unreadable: {e}", skipped
    try:
        return resolve(text, found, skipped), None, skipped
    except SettingsError as e:
        return None, str(e), skipped


def daemon() -> dict | None:
    """The running daemon's runtime file, or None."""
    return read_runtime(runtime_path())


def _seq(d: dict) -> int:
    seq = d.get("signal_seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


def signal(sig: int, *, timeout: float = 1.0, poll: float = 0.05) -> tuple[int, str | None]:
    """Send `sig` to the daemon's pid (never its process group: the filter
    child dies on SIGUSR1) and wait for it to bump `signal_seq`. Returns
    `(pid, pause_refused)`."""
    path = runtime_path()
    if path is None:
        raise DaemonNotRunning("can't tell whether floficam is running: XDG_RUNTIME_DIR is not set")
    d = read_runtime(path)
    if d is None:
        raise DaemonNotRunning("floficam is not running")
    pid = d["pid"]
    seq = _seq(d)
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        raise DaemonNotRunning("floficam is not running") from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll)
        now = read_runtime(path)
        if now is not None and now.get("pid") == pid and _seq(now) > seq:
            return pid, now.get("pause_refused")
    raise DaemonNoAnswer(pid)
