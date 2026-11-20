"""The `floficam` command: argparse and output over control.py.

No GStreamer at import time: `floficam setup` re-runs this script under
sudo without the wrapper's GI environment, so `gi`, `floficam.main`,
`floficam.cameras` and `floficam.pwinput` are imported only inside `run`
and `cameras`."""
import argparse
import signal
import sys

from floficam import control
from floficam.device import LABEL, NO_DEVICE, find_device
from floficam.filter.settings import Knobs, SettingsError, knob_validators, state_dir
from floficam.runtime import read_runtime, runtime_path
from floficam.setup import run_setup

OVERVIEW = """\
floficam: a virtual camera that shows your webcam through lo-fi effects.

commands, one example each:
  floficam setup                    create the virtual camera device (asks for sudo)
  floficam run                      run the camera feed in this terminal (Ctrl-C stops it)
  floficam cameras                  list the cameras it can read from
  floficam status                   is it running, which device, camera and preset
  floficam preset vhs               pick a preset (without a name: list them)
  floficam set picture.width=320    change one knob on top of the preset
  floficam reset                    drop your knob changes, keep the preset
  floficam knobs                    every knob: what it accepts, default, current value
  floficam pause                    freeze the picture on the current frame
  floficam resume                   unfreeze it

Settings changes apply to a running camera within a moment.
`floficam COMMAND -h` shows a command's options."""

REFUSALS = {
    "input_down": "pause refused: the camera isn't in use (pause works while something is streaming)",
    "filter_not_ready": "pause refused: effects not ready yet",
    "no_fresh_frame": "pause refused: no recent camera frame",
}
NOT_RUNNING_HINT = "start it with `floficam run`, or `systemctl --user start floficam` if you use the service"


def _err(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _overrides(doc: dict) -> list[str]:
    overrides = doc.get("overrides")
    if not isinstance(overrides, dict):
        return []
    out = []
    for group, knobs in overrides.items():
        if isinstance(knobs, dict):
            out.extend(f"{group}.{knob}={_fmt(v)}" for knob, v in knobs.items())
    return out


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


# --- commands ------------------------------------------------------------------


def cmd_setup(args) -> int:
    return run_setup(args.label)


def cmd_run(args) -> int:
    live = control.daemon()
    if live is not None:
        _err(f"floficam is already running (pid {live['pid']}); stop it with "
             f"`systemctl --user stop floficam` or Ctrl-C in its terminal")
        return 1
    argv = ["--camera", args.camera or "auto"]
    if args.device:
        argv += ["--device", args.device]
    for prop in args.stream_property:
        argv += ["--stream-property", prop]
    if args.hold is not None:
        argv += ["--hold", args.hold]
    from floficam.main import main as run_daemon
    return run_daemon(argv)


def cmd_cameras(args) -> int:
    from floficam import cameras
    output = find_device(None)
    devices = cameras.list_devices()
    usable = cameras.usable(devices, output)
    if not usable:
        print("no cameras found")
        return 0
    auto = cameras.pick(output, list_devices=lambda: devices)
    rows = [("", "CAMERA", "NAME (for --camera)")]
    rows += [("*" if cam == auto else "", cam.description, cam.target) for cam in usable]
    for line in _table(rows):
        print(line)
    print()
    print("The * marks the one `--camera auto` (the default) uses.")
    return 0


def cmd_status(args) -> int:
    path = runtime_path()
    live = read_runtime(path) if path is not None else None
    if path is None:
        state = "can't tell: XDG_RUNTIME_DIR is not set, so the daemon's runtime file can't be found"
    elif live is not None:
        state = f"running (pid {live['pid']})"
    elif path.exists():
        state = "not running (it stopped without cleaning up; the pid it left is gone)"
    else:
        state = "not running (no daemon has started, or it exited)"
    rows = [("floficam:", state)]
    if live is not None:
        rows.append(("device:", str(live.get("device"))))
        rows.append(("camera:", str(live.get("camera"))))
        rows.append(("status:", str(live.get("status") or "-")))
    else:
        rows.append(("device:", find_device(None) or NO_DEVICE))

    sd = state_dir()
    try:
        doc = control.read_filter(sd)
    except SettingsError:
        doc = None
    if doc is not None:
        rows.append(("preset:", str(doc.get("preset", "none"))))
        rows.append(("overrides:", ", ".join(_overrides(doc)) or "none"))
    for line in _table(rows):
        print(line)

    _, error, skipped = control.effective(sd)
    for preset_id, reason in skipped.items():
        print(f"skipped preset {preset_id}: {reason}")
    if error is not None:
        print(f"settings error: {error}")
    return 0


def _echo_settings(doc: dict) -> None:
    for line in _table([("preset:", str(doc.get("preset", "none"))),
                        ("overrides:", ", ".join(_overrides(doc)) or "none")]):
        print(line)
    if runtime_path() is not None and control.daemon() is None:
        print("floficam isn't running: this applies when it starts")


def _settings_write(fn, *fn_args, knob_hint=False) -> int:
    try:
        doc = fn(state_dir(), *fn_args)
    except control.BrokenFilter as e:
        _err(f"your settings file is broken, so nothing was changed: {e}",
             f"`floficam preset none` starts it over (or delete {state_dir() / control.FILTER})")
        return 1
    except (SettingsError, ValueError) as e:
        _err(str(e), *(["`floficam knobs` lists what each knob accepts"] if knob_hint else []))
        return 1
    except OSError as e:
        _err(f"could not write the settings: {e}")
        return 1
    _echo_settings(doc)
    return 0


def cmd_preset(args) -> int:
    if args.id is not None:
        return _settings_write(control.select_preset, args.id)
    sd = state_dir()
    try:
        found, skipped = control.presets(sd)
    except SettingsError as e:
        _err(str(e))
        return 1
    try:
        doc = control.read_filter(sd)
    except SettingsError as e:
        _err(f"settings error: {e}")
        doc = {}
    active = doc.get("preset")
    rows = []
    for preset in found.values():
        tags = [] if preset.builtin else ["your preset"]
        if preset.id == active and _overrides(doc):
            tags.append("with your knob changes")
        rows.append(("*" if preset.id == active else " ", preset.id, preset.name,
                     f"({', '.join(tags)})" if tags else ""))
    for line in _table(rows):
        print(line)
    print()
    print("The * marks the active preset; pick another with `floficam preset ID`.")
    for preset_id, reason in skipped.items():
        _err(f"skipped preset {preset_id}: {reason}")
    return 0


def cmd_set(args) -> int:
    return _settings_write(control.set_knobs, args.assignments, knob_hint=True)


def cmd_reset(args) -> int:
    return _settings_write(control.reset)


def cmd_knobs(args) -> int:
    resolved, error, _ = control.effective(state_dir())
    try:
        overrides = control.read_filter(state_dir()).get("overrides") or {}
    except SettingsError:
        overrides = {}
    defaults = Knobs()
    rows = [("KNOB", "ALLOWED", "DEFAULT", "CURRENT")]
    for group, validators in knob_validators().items():
        for knob, validate in validators.items():
            default = _fmt(getattr(getattr(defaults, group), knob))
            if resolved is None:
                current = "?"
            else:
                current = _fmt(getattr(getattr(resolved.knobs, group), knob))
                if isinstance(overrides.get(group), dict) and knob in overrides[group]:
                    current += " *"
            rows.append((f"{group}.{knob}", validate.allowed, default, current))
    for line in _table(rows):
        print(line)
    print()
    if resolved is not None:
        print(f"CURRENT is preset {resolved.preset_id}; * marks your changes (`floficam reset` drops them).")
    else:
        print(f"settings error: {error}")
    print("Change one with `floficam set GROUP.KNOB=VALUE`.")
    return 0


def _signal(sig: int) -> tuple[int, str | None] | None:
    try:
        return control.signal(sig)
    except control.DaemonNotRunning as e:
        _err(str(e), NOT_RUNNING_HINT)
    except control.DaemonNoAnswer as e:
        _err(f"signal sent to pid {e.pid} but the daemon didn't confirm")
    except OSError as e:
        _err(f"could not signal floficam: {e.strerror or e}")
    return None


def cmd_pause(args) -> int:
    answer = _signal(signal.SIGUSR1)
    if answer is None:
        return 1
    pid, refused = answer
    if refused is None:
        print(f"paused (pid {pid})")
        return 0
    if refused == "already_frozen":
        print("already paused")
        return 0
    _err(REFUSALS.get(refused, f"pause refused: {refused}"))
    return 1


def cmd_resume(args) -> int:
    answer = _signal(signal.SIGUSR2)
    if answer is None:
        return 1
    print(f"resumed (pid {answer[0]})")
    return 0


# --- parser ----------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: {message}\n")


def _parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="floficam", add_help=False)
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, func, summary, description=None):
        p = sub.add_parser(name, help=summary, description=description or summary)
        p.set_defaults(func=func)
        return p

    p = add("setup", cmd_setup, "create the virtual camera device",
            "Create the virtual camera device floficam writes to. Does nothing if it already exists. "
            "Needs root: it re-runs itself with sudo. The device lasts until reboot; "
            "on NixOS, programs.floficam.loopback.enable makes it permanent.")
    p.add_argument("--label", default=LABEL, help=argparse.SUPPRESS)

    p = add("run", cmd_run, "run the camera feed in this terminal",
            "Run the camera feed in the foreground until Ctrl-C. Refuses if floficam is already running.")
    p.add_argument("--device", help="the virtual camera device (default: the one named floficam)")
    p.add_argument("--camera", help="'auto' (default: the first real camera), a NAME from `floficam cameras`, "
                                    "or 'external' (wait for another program to connect a camera)")
    p.add_argument("--stream-property", action="append", default=[], metavar="KEY=VALUE",
                   help="extra PipeWire stream property; repeatable")
    p.add_argument("--hold", metavar="SECONDS",
                   help="how long to keep showing the last frame when the camera stops delivering, before colour bars (default 10; 0 = bars after 0.2 s, the old behaviour; inf = never)")

    add("cameras", cmd_cameras, "list the cameras floficam can read from",
        "List the real cameras PipeWire offers (virtual ones, floficam's own included, are left out). "
        "* marks the one `--camera auto` uses.")
    add("status", cmd_status, "show whether floficam is running, and its device, camera and preset")

    p = add("preset", cmd_preset, "list presets, or pick one",
            "Without ID: list presets, the active one marked. With ID: make it active "
            "(this drops your knob changes).")
    p.add_argument("id", nargs="?", metavar="ID", help="the preset to use, e.g. vhs")

    p = add("set", cmd_set, "change knobs on top of the active preset",
            "Change knobs on top of the active preset, e.g. `floficam set picture.width=320 analog.grain=0.4`. "
            "Nothing is changed if any value is refused; `floficam knobs` lists what each knob accepts.")
    p.add_argument("assignments", nargs="+", metavar="GROUP.KNOB=VALUE")

    add("reset", cmd_reset, "drop your knob changes, keep the preset")
    add("knobs", cmd_knobs, "list every knob: what it accepts, its default and its current value")
    add("pause", cmd_pause, "freeze the picture on the current frame",
        "Freeze the picture on the current frame. Works while something is using the camera.")
    add("resume", cmd_resume, "unfreeze the picture")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(OVERVIEW)
        return 0
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return 0 if e.code in (0, None) else 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
