# floficam CLI and imperative setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `floficam` command a newcomer can drive: `setup` makes the virtual camera, `run` feeds it from a camera it picks itself, and `preset`/`set`/`pause`/`status` control a running daemon, whether it's the user service or `floficam run`.

**Architecture:** Three small library modules (device lookup, the daemon's runtime file, camera discovery) serve both the daemon and a new `floficam.cli`. Settings writes are validated by `filter/settings.py` before an atomic rename, so a typo is refused instead of turning the camera into bars. `setup` adds a device through v4l2loopback's control ioctl when the module is loaded, or modprobes it, and re-runs itself under sudo when not root.

**Tech Stack:** Python 3 stdlib (argparse, struct, fcntl), PyGObject + GStreamer `DeviceMonitor`, pytest, NixOS VM test.

**Spec:** `docs/superpowers/specs/2026-11-20-floficam-cli-design.md` (approved by florek 2026-11-20). Its parent is the monorepo's P2 spec, `/var/lib/datacombs/nix/.worktrees/feat/floficam-p1/docs/superpowers/specs/2026-11-19-floficam-extraction-design.md`, sections "CLI" and "Camera selection".

## Global Constraints

- Repo `/datacombs/floficam`, trunk `master`; work on `feat/cli` in `/datacombs/floficam/.worktrees/feat/cli`. Commits are signed by the worktree config; never `--no-gpg-sign`, no Co-Authored-By lines.
- Run tests with `nix develop -c pytest -q` from the worktree (the dev shell sets PYTHONPATH and the GStreamer paths). Builds and the VM test run in the foreground.
- No new runtime dependencies: stdlib plus what the package already has (PyGObject, GStreamer base/good, pipewire, numpy).
- The device label is `floficam`. The runtime file is `$XDG_RUNTIME_DIR/floficam/daemon.json` with keys `pid`, `start_time`, `device`, `camera`, `status`, `signal_seq`, `pause_refused`.
- `floficam-daemon` keeps its name and flags; the service keeps calling it. The `hm-assert` control ExecStart in `flake.nix` (`… floficam-daemon --device /dev/video10 --camera external --stream-property firefox-sandbox.lane=camera-feed`) must stay byte-identical: westfall depends on it.
- No-device text, verbatim: `no floficam device: create one with \`floficam setup\`, or enable programs.floficam.loopback on NixOS`.
- Missing-module text, verbatim: `v4l2loopback is not installed: install your distribution's v4l2loopback package, or on NixOS set programs.floficam.loopback.enable = true`.
- Nothing the daemon does on the GLib main thread may block on PipeWire or DeviceMonitor I/O (pwinput.py's R9: blocking there froze the 30 Hz tick). Nothing on the tick path may raise on an I/O error: a raise quits the daemon through `_fail_from_callback`.
- `nix fmt -- --ci` and `nix shell --inputs-from . nixpkgs#ruff -c ruff check --select F,E9 .` stay clean.
- User-facing help and messages contain no spec paths or internal names (no "R12", "lane", "Task").

## Review Focus

- A `set` value of the wrong type that still parses (`picture.width=320px`, `analog.interlace=1`, `codec.kind=JPEG`) is refused with the validator's message, exit 1, and `filter.json` is byte-identical afterwards. String choices that look like JSON (`picture.colour=true`, `picture.palette=2`) are accepted. (Task 4 tests.)
- A runtime file left by a killed daemon, or naming a pid now owned by another process, reads as "not running": `status` says so, and `pause` exits 1 without signalling anything. (Task 1 tests.)
- `XDG_RUNTIME_DIR` unset (sudo shells, ssh without a session): the daemon runs without a runtime file, and `status`/`pause` say what's missing instead of raising. (Task 1 and Task 4 tests.)
- `floficam run` while the service (or another `run`) is live refuses with the other pid and how to stop it, instead of fighting over the device. (Task 4 test.)
- `auto` never picks a screencast or other virtual `Video/Source`: with the webcam unplugged it shows bars, never the screen. (Task 2 test.)

---

### Task 1: device lookup, runtime file, daemon start-up messages

**Files:**
- Create: `floficam/device.py`, `floficam/runtime.py`, `tests/conftest.py`
- Modify: `floficam/main.py` (`parse_args`, `main`, Runner's signal hook)
- Test: `tests/test_device.py`, `tests/test_runtime.py`, `tests/test_main.py`

**Interfaces:**
- Produces:
  - `device.LABEL = "floficam"`
  - `device.find_device(explicit: str | None, label: str = LABEL, *, sysfs: Path = Path("/sys/class/video4linux"), dev: Path = Path("/dev")) -> str | None`: returns `explicit` if given (even when missing, so the open error names it). Otherwise, only when `label == LABEL`, `dev/"floficam"` if it exists. Otherwise `dev/<entry>` for the first sysfs entry (sorted by name) whose `name` file, stripped, equals `label`. Otherwise None.
  - `runtime.runtime_path(env: Mapping[str, str] = os.environ) -> Path | None`: `$XDG_RUNTIME_DIR/floficam/daemon.json`; None if the variable is unset or empty.
  - `runtime.proc_start_time(pid: int, *, proc: Path = Path("/proc")) -> int | None`: field 22 of `proc/<pid>/stat`, counted from the text after the last `)`; None if unreadable.
  - `runtime.write_runtime(path: Path, *, pid: int, start_time: int, device: str, camera: str, status: str, signal_seq: int = 0, pause_refused: str | None = None) -> None`: mkdir parents, write a temp file in the same dir, `os.replace`.
  - `runtime.read_runtime(path: Path | None, *, proc: Path = Path("/proc")) -> dict | None`: None when `path` is None, missing or unparseable, or when `proc_start_time(pid)` isn't the stored `start_time` (covers both a dead pid and a reused one).
  - `runtime.remove_runtime(path: Path | None, *, pid: int = os.getpid()) -> None`: removes the file only if it parses and its `pid` equals `pid`.
  - `runtime.RuntimeNotify(path: Path | None, send: Callable[[str], bool] | None, *, pid, start_time, device, camera)`, with methods:
    - `notify(status: str) -> None`: rewrite the file if `path`, then call `send` if `send`.
    - `set_camera(camera: str) -> None`: rewrite with a new camera field.
    - `signal_result(refusal: str | None) -> None`: increment `signal_seq`, set `pause_refused`, rewrite.
    
    Every write catches OSError and logs `runtime file: <error>` once per distinct message; it never raises.
- Runner (main.py) gains `on_signal_result: Callable[[str | None], None] = lambda r: None`. `_on_user_signal` calls it after `on_pause`, with the lowercase refusal name or None (and with None on SIGUSR2), before `_set_state`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/conftest.py: autouse fixture
@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("FLOFICAM_STATE_DIR", str(tmp_path / "state"))

# tests/test_device.py
def test_explicit_wins(tmp_path):
    assert find_device("/dev/videoX", sysfs=tmp_path / "s", dev=tmp_path / "d") == "/dev/videoX"

def test_symlink_then_label(tmp_path):
    sysfs, dev = tmp_path / "s", tmp_path / "d"
    for n, name in (("video0", "apple-isp"), ("video3", "floficam\n")):
        (sysfs / n).mkdir(parents=True); (sysfs / n / "name").write_text(name)
    dev.mkdir()
    assert find_device(None, sysfs=sysfs, dev=dev) == str(dev / "video3")
    (dev / "floficam").touch()
    assert find_device(None, sysfs=sysfs, dev=dev) == str(dev / "floficam")

def test_symlink_ignored_for_other_label(tmp_path):
    # dev/floficam exists; sysfs video7 is named "vmtest"
    assert find_device(None, "vmtest", sysfs=sysfs, dev=dev) == str(dev / "video7")
    # with no vmtest entry: None, not dev/floficam

def test_none_found(tmp_path):
    assert find_device(None, sysfs=tmp_path / "missing", dev=tmp_path) is None

# tests/test_runtime.py
def test_round_trip_and_stale(tmp_path):
    # fake proc/<pid>/stat with a comm containing spaces and ")" : "123 (a) b) S 1 ... <starttime> ..."
    # write_runtime(..., start_time=<that field 22>) -> read_runtime(p, proc=proc)["status"] == "camera=live filter=vhs"
    # rewrite stat with a different starttime (pid reused) -> read_runtime(...) is None

def test_dead_pid_and_garbage(tmp_path): ...       # no proc/<pid>; then a file containing "{": both None
def test_remove_leaves_other_pids_file(tmp_path): ...
def test_runtime_path_unset():
    assert runtime_path({}) is None
    assert runtime_path({"XDG_RUNTIME_DIR": "/run/user/1"}) == Path("/run/user/1/floficam/daemon.json")
def test_notify_writes_then_sends(tmp_path): ...    # status in file; send called with it
def test_notify_without_sender_is_silent(tmp_path, capsys): ...   # send=None: file written, nothing logged
def test_runtime_write_failure_does_not_raise(tmp_path, capsys):
    n = RuntimeNotify(tmp_path / "f" / "daemon.json", None, pid=1, start_time=1, device="d", camera="c")
    (tmp_path / "f").write_text("a file, not a dir")
    n.notify("a"); n.notify("b")
    assert capsys.readouterr().err.count("runtime file:") == 1
def test_signal_result_bumps_seq(tmp_path): ...      # signal_seq 0 -> 1, pause_refused "input_down"; again with None -> 2, null

# tests/test_main.py (additions)
def test_no_device_exits_with_hint(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "find_device", lambda explicit: None)
    assert main_mod.main([]) == 1
    assert "no floficam device: create one with `floficam setup`" in capsys.readouterr().err

def test_help_has_no_internal_references(capsys):
    with pytest.raises(SystemExit): main_mod.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "docs/" not in out and "spec" not in out

def test_failed_start_leaves_no_runtime_file(): ...  # existing producer_raising pattern; no daemon.json under the tmp XDG_RUNTIME_DIR
def test_signal_hook_called_with_refusal(): ...      # Runner with input down, SIGUSR1 -> hook got "input_down"; SIGUSR2 -> None
```

- [ ] **Step 2: Run them and expect failures.** `nix develop -c pytest -q tests/test_device.py tests/test_runtime.py tests/test_main.py` fails with import errors or missing behaviour.

- [ ] **Step 3: Implement** `device.py` and `runtime.py`, then in `main.py`:
  - `--device` defaults to None and resolves through `find_device`. If that returns None, log the no-device text and return 1. If an explicit path fails to open, append ` (create the device with \`floficam setup\`)` to the error.
  - `parse_args` gets a user-facing description: "Feed a v4l2loopback device with your camera through floficam's effects." `--camera` help lists `auto`, `external`, or a PipeWire node name.
  - Once the device opens, build a `RuntimeNotify` with `send = _make_notify()` if `NOTIFY_SOCKET` is set, else None. Call `notify("starting")`, pass `.notify` to Runner and `.signal_result` as `on_signal_result`, and call `remove_runtime(path)` in `main`'s `finally`.
  - Without `XDG_RUNTIME_DIR`, `path` is None: log one line and otherwise carry on unchanged.

- [ ] **Step 4: Run the full suite.** `nix develop -c pytest -q`: everything passes.

- [ ] **Step 5: Commit.** `feat(daemon): find the device by label, say how to create it, runtime file`

### Task 2: `--camera auto`

**Files:**
- Create: `floficam/cameras.py`
- Modify: `floficam/pwinput.py` (camera resolution inside the start job; public `is_safe_target`), `floficam/main.py` (`_camera_arg`, `make_input`), `nix/hm-module.nix` (`camera` default `"auto"`, description)
- Test: `tests/test_cameras.py`, `tests/test_pwinput.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `device.find_device` and `RuntimeNotify.set_camera` (Task 1).
- Produces:
  - `pwinput.is_safe_target(s: str) -> bool`: `_SAFE.fullmatch`. Use it everywhere; don't copy the regex.
  - `cameras.Camera` (frozen dataclass): `target: str`, `description: str`, `path: str | None`
  - `cameras.list_devices() -> list[dict[str, str]]`: the properties of each PipeWire `Video/Source` device, as strings, plus `"description"` from `Gst.Device.get_display_name()`. It runs `Gst.DeviceMonitor` with a `Video/Source` filter and every provider except `pipewiredeviceprovider` hidden (`hide_provider`): `start()`, then `get_devices()`, then `stop()`.
  - `cameras.usable(devices: list[Mapping[str, str]], output_device: str | None) -> list[Camera]`. It keeps only hardware cameras, meaning `device.api` is `v4l2` or `libcamera`, or `api.v4l2.path` is present, or any `api.libcamera.*` key is present. From those it drops:
    - any device whose `api.v4l2.cap.driver` is `v4l2 loopback`;
    - any device whose `api.v4l2.path` or `device.path` has the same `os.path.realpath` as `output_device`.
    
    The target is `node.name` if `is_safe_target`, else `object.serial` if `is_safe_target`; a device with neither is dropped. Order is kept.
  - `cameras.pick(output_device: str | None, *, list_devices=list_devices) -> Camera | None`: the first of `usable(...)`.

- [ ] **Step 1: Write the failing tests**

```python
REAL = {"device.api": "v4l2", "node.name": "v4l2_input.platform-isp", "api.v4l2.cap.driver": "apple-isp",
        "api.v4l2.path": "/dev/video0", "description": "FaceTime HD"}
LOOP = {"device.api": "v4l2", "node.name": "v4l2_input.loop", "api.v4l2.cap.driver": "v4l2 loopback",
        "api.v4l2.path": "/dev/video10", "description": "floficam"}
SCREEN = {"node.name": "xdpw_screencast", "media.class": "Video/Source", "description": "Screen"}

def test_loopbacks_and_output_skipped():
    assert [c.target for c in usable([LOOP, REAL], "/dev/video10")] == ["v4l2_input.platform-isp"]
    other = dict(REAL, **{"api.v4l2.cap.driver": "uvcvideo"})
    assert usable([other], "/dev/video0") == []          # same path as the output

def test_screencast_dropped():
    assert [c.target for c in usable([SCREEN, REAL], None)] == ["v4l2_input.platform-isp"]
    assert usable([SCREEN], None) == []

def test_serial_fallback_and_nameless_dropped():
    assert usable([{"device.api": "v4l2", "node.name": "cam with space", "object.serial": "42"}], None)[0].target == "42"
    assert usable([{"device.api": "v4l2", "node.name": "cam with space"}], None) == []
    assert usable([{"device.api": "v4l2", "description": "?"}], None) == []

def test_pick_none():
    assert pick("/dev/video10", list_devices=lambda: [LOOP, SCREEN]) is None

def test_auto_resolves_in_the_worker_job():
    # CameraInput under auto with a fake worker that records submitted jobs:
    # list_devices is not called when make_input(...)/start() return; it is
    # called once when the recorded job runs, once per input start. The result
    # reaches the main thread via GLib.idle_add (patched to call immediately):
    # the log line is "camera auto: using FaceTime HD (v4l2_input.platform-isp)"
    # or "camera auto: no camera found, showing bars", and set_camera got
    # "auto: FaceTime HD (v4l2_input.platform-isp)" / "auto: none (bars)".
```

- [ ] **Step 2: Run them and expect failures.**
- [ ] **Step 3: Implement** `cameras.py`, `is_safe_target`, and `_camera_arg` accepting `auto`.
  - Under `auto`, camera resolution runs inside the CameraInput start job on the pwinput worker, before PLAYING, under the existing job deadline and watchdog. A wedged DeviceMonitor is reported as overdue, the same way a wedged state change is.
  - `make_input` and `CameraInput.start()` do no PipeWire or DeviceMonitor I/O on the calling thread. The mechanism is the implementer's choice; one way is to set pipewiresrc's `target-object`/`autoconnect` properties on the already-built pipeline from the job.
  - With no camera found, the input stays unlinked (bars).
  - The result goes back to the main thread through `GLib.idle_add`, for the log line and `set_camera`.
  - In `hm-module.nix` the default becomes `"auto"`, and the description lists the three forms.
- [ ] **Step 4: Probe on westfall (read-only).** Run `nix develop -c python -c 'from floficam.cameras import list_devices, usable; d=list_devices(); [print(x.get("device.api"), x) for x in d]; print(usable(d, "/dev/video10"))'`. Expect the Apple ISP camera's PipeWire node in `usable`. If its property names differ from `device.api`, `api.v4l2.cap.driver`, `api.v4l2.path` or `node.name`, fix the code and the fixtures to match the real names, and say so in the report.
- [ ] **Step 5: Run the full suite, plus `nix build -L --no-link .#checks.aarch64-linux.hm-assert`.** The westfall control must still pass; everything green.
- [ ] **Step 6: Commit.** `feat(daemon): --camera auto picks a hardware camera off the main thread; HM default auto`

### Task 3: `setup`

**Files:**
- Create: `floficam/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `device.find_device(None, label)` (Task 1).
- Produces:
  - `setup.CTL_PATH = "/dev/v4l2loopback"`, `setup.CTL_ADD = 0x40487E01` (`_IOW('~', 1, 72)`)
  - `setup.pack_config(label: str) -> bytearray`: `struct.pack("<ii32sIIIIiiii", -1, -1, label.encode(), 0, 0, 0, 0, 2, -1, 0, 1)`. The fields are output_nr, capture_nr, card_label, min/max width/height (0 means the module default), max_buffers=2, max_openers=-1 (default), debug=0, announce_all_caps=1. A label over 31 bytes raises ValueError.
  - `setup.run_setup(label: str = LABEL, *, find=find_device, is_root=lambda: os.geteuid() == 0, ctl_exists=lambda: os.path.exists(CTL_PATH), ctl_add=_ctl_add, modprobe=_modprobe, reexec=_sudo_reexec, wait=_wait_for, out=print) -> int`
    - `_ctl_add(buf: bytearray) -> int` calls `fcntl.ioctl(fd, CTL_ADD, buf, True)` (mutable buffer, so the call returns the syscall's int, the new device number N).
    - `_modprobe(args) -> tuple[int, str]` returns the exit code and stderr as text, using `shutil.which("modprobe") or "/sbin/modprobe"`.
    - `_wait_for(predicate, timeout) -> bool` polls every 0.1 s.
  - Decision order:
    1. **Device exists:** print `floficam device already exists: <path>` and return 0.
    2. **Not root:** print `floficam setup needs root, running: sudo <cmd>`, then call `reexec([...])`. It `os.execvp`s `sudo -- <realpath(sys.argv[0])> setup --label <label>` and never returns.
    3. **Root, control device present:** `n = ctl_add(pack_config(label))`. On OSError, print `could not add a device: <strerror>` and return 1. Otherwise wait up to 5 s for `/dev/video<n>`, then print `created /dev/video<n>` and return 0.
    4. **Root, no control device:** run `modprobe(["v4l2loopback", "devices=1", f"card_label={label}", "exclusive_caps=0", "max_buffers=2"])`.
       - Nonzero with `not found` in stderr: print the missing-module text and return 1.
       - Any other nonzero: print `modprobe failed: <stderr stripped>` and return 1.
       - Success: wait up to 5 s for `find_device(None, label)` and print `created <path>`.
    
    Whenever a wait times out, print `the module accepted the request but no device named <label> appeared` and return 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_constants_and_layout():
    assert CTL_ADD == 0x40487E01
    buf = pack_config("floficam")
    assert len(buf) == 72 and buf[8:17] == b"floficam\0"
    i32 = lambda o: int.from_bytes(buf[o:o + 4], "little", signed=True)
    assert (i32(0), i32(4), i32(56), i32(60), i32(64), i32(68)) == (-1, -1, 2, -1, 0, 1)
    with pytest.raises(ValueError): pack_config("x" * 32)

def test_exists_is_a_no_op():
    calls = []
    assert run_setup(find=lambda *_: "/dev/video3", ctl_add=calls.append, out=calls.append) == 0
    assert calls == ["floficam device already exists: /dev/video3"]

def test_not_root_reexecs_with_label(): ...           # reexec got ["sudo", "--", <exe>, "setup", "--label", "floficam"]
def test_ctl_add_then_wait(): ...                     # fake ctl_add returns 11 -> prints "created /dev/video11"
def test_ctl_add_error_reports_errno():
    def boom(_): raise OSError(errno.ENOSPC, "No space left on device")
    lines = []
    assert run_setup(find=lambda *_: None, is_root=lambda: True, ctl_exists=lambda: True,
                     ctl_add=boom, out=lines.append) == 1
    assert lines[-1] == "could not add a device: No space left on device"
def test_modprobe_missing_module_hint(): ...          # (1, "modprobe: FATAL: Module v4l2loopback not found in directory /lib/modules/x\n") -> missing-module text, 1
def test_modprobe_other_failure_shows_stderr(): ...   # (1, "modprobe: ERROR: could not insert 'v4l2loopback': Key was rejected by service\n") -> contains "Key was rejected", not the hint, 1
def test_device_never_appears(): ...                  # wait -> False: exit 1 with the "accepted … no device" text
```

- [ ] **Step 2: Run them and expect failures.**
- [ ] **Step 3: Implement** with `os.open(CTL_PATH, os.O_RDWR)` and the functions above. Task 5's VM test proves the real ioctl.
- [ ] **Step 4: Run the full suite:** green.
- [ ] **Step 5: Commit.** `feat: floficam setup creates the virtual camera (control ioctl or modprobe, sudo re-exec)`

### Task 4: the `floficam` command

**Files:**
- Create: `floficam/control.py` (settings and daemon control, no printing), `floficam/cli.py` (argparse and output)
- Modify: `floficam/filter/settings.py` (each validator gets an `allowed: str` attribute; public `knob_validators()`), `pyproject.toml` (`floficam = "floficam.cli:main"`), `nix/package.nix` (`mainProgram = "floficam"`)
- Test: `tests/test_control.py`, `tests/test_cli.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes:
  - Task 1: `find_device`, `runtime_path`, `read_runtime`.
  - Task 2: `cameras.list_devices`, `usable`, `pick`.
  - Task 3: `setup.run_setup`.
  - `floficam.main.main`.
  - From `settings`: `load_presets`, `builtin_presets_path`, `state_dir`, `resolve`, `SettingsError`.
- Produces:
  - `settings.knob_validators() -> dict[str, dict[str, Callable]]`. Each validator has `.allowed`: `_choice` gives `"1280, 640, 320, 256, 160"`, `_range` gives `"0 to 20"`, `_bool` gives `"true, false"`.
  - `control.read_filter(state: Path) -> dict` (missing file: `{"version": 1, "preset": "none"}`).
  - `control.write_filter(state: Path, doc: dict) -> None` (mkdir, temp file, `os.replace`).
  - `control.parse_assignment(text: str) -> tuple[str, str, str]`: `group.knob=value`, value as the raw string. Anything without exactly one `.` before `=` raises `ValueError("expected GROUP.KNOB=VALUE, e.g. picture.width=320")`.
  - `control.set_knobs(state: Path, assignments: list[str]) -> dict` coerces per knob. It tries the raw string first, then `json.loads(raw)` if that parses, and keeps the first form the knob's validator accepts. If neither passes, it raises the validator's SettingsError for the parsed form (or for the raw string when it doesn't parse). It merges into the overrides, validates the whole doc with `resolve(json.dumps(doc), presets, skipped)`, then writes. On any error the file is untouched.
  - `control.select_preset(state: Path, preset_id: str) -> dict` (drops overrides) and `control.reset(state: Path) -> dict`: validate the same way, then write.
  - `control.daemon() -> dict | None`: `read_runtime(runtime_path())`.
  - `control.signal(sig: int, *, timeout: float = 1.0, poll: float = 0.05) -> tuple[int, str | None]`:
    - reads `signal_seq`, sends `sig` to the daemon pid only, then polls the runtime file until `signal_seq` increases, and returns `(pid, pause_refused)`;
    - raises `DaemonNotRunning` (its message says whether the runtime dir is unknown or nothing is running);
    - raises `DaemonNoAnswer(pid)` on timeout.
  - `cli.main(argv: list[str] | None = None) -> int`.

Commands in `cli.main`:
- **No args or `-h`:** an overview listing every command with one example each.
- **`setup [--label]`** (the label option is hidden from help): `run_setup`.
- **`run [--device] [--camera auto] [--stream-property]`:** refuses when `control.daemon()` is live, with `floficam is already running (pid N); stop it with \`systemctl --user stop floficam\` or Ctrl-C in its terminal`. Otherwise it calls `floficam.main.main([...])`, with `--camera auto` unless one is given.
- **`cameras`:** each usable camera's description and target, with `*` on the one `auto` picks; `no cameras found` when empty.
- **`status`:**
  - running or not running, and why;
  - the device (the runtime file's, else `find_device(None)`);
  - the camera field;
  - the daemon's status line;
  - the preset and overrides from `filter.json`;
  - the result of running `load_presets(builtin_presets_path(), state_dir()/"presets.json")` and `resolve(...)` locally: `settings error: <msg>` when resolve raises, and `skipped preset <id>: <reason>` per skipped entry.
  
  Exits 0.
- **`preset`:** presets in order, the active one marked, user presets tagged.
- **`preset ID`, `set A=B…`, `reset`:** write, then echo the new preset and overrides.
- **`knobs`:** a table of knob, allowed values, default and current value.
- **`pause`:**
  - `None` → `paused (pid N)`, exit 0
  - `already_frozen` → `already paused`, exit 0
  - `input_down` → `pause refused: the camera isn't in use (pause works while something is streaming)`, exit 1
  - `filter_not_ready` → `pause refused: effects not ready yet`, exit 1
  - `no_fresh_frame` → `pause refused: no recent camera frame`, exit 1
  - `DaemonNoAnswer` → `signal sent to pid N but the daemon didn't confirm`, exit 1
- **`resume`:** `resumed (pid N)`; on no answer, the same no-confirm text and exit 1.

Errors go to stderr, exit 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_set_refuses_wrong_types_and_leaves_file(tmp_path):
    write_filter(tmp_path, {"version": 1, "preset": "vhs"})
    before = (tmp_path / "filter.json").read_bytes()
    for bad in ("picture.width=320px", "analog.interlace=1", "codec.kind=JPEG", "nope.knob=1", "width=320"):
        with pytest.raises((SettingsError, ValueError)): set_knobs(tmp_path, [bad])
    assert (tmp_path / "filter.json").read_bytes() == before

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
    with pytest.raises(SettingsError): select_preset(tmp_path, "nope")

def test_validators_describe_themselves():
    v = knob_validators()
    assert v["picture"]["width"].allowed == "1280, 640, 320, 256, 160"
    assert v["temporal"]["stalls_per_min"].allowed == "0 to 20"

def test_pause_signals_only_the_daemon_pid(tmp_path):
    # a child python process whose SIGUSR1 handler rewrites the runtime file
    # with signal_seq+1 and pause_refused null; the runtime file names its pid
    # and real start_time; cli.main(["pause"]) == 0 and prints "paused (pid N)"
def test_pause_refused_exits_1(): ...           # the child writes pause_refused "input_down": exit 1, the input_down text
def test_pause_no_answer_exits_1(): ...         # the child doesn't rewrite; short timeout injected: exit 1, no-confirm text
def test_pause_not_running(capsys): ...          # no runtime file: exit 1, "floficam is not running"
def test_status_without_runtime_dir(monkeypatch, capsys): ...   # XDG_RUNTIME_DIR unset: exit 0, says it can't tell
def test_status_shows_settings_error_and_skipped(capsys): ...   # filter.json names an unknown preset, user presets.json has one bad entry
def test_run_refuses_when_live(monkeypatch, capsys): ...         # control.daemon() -> {"pid": 4242, ...}: exit 1, mentions 4242
def test_no_args_prints_overview(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for cmd in ("setup", "run", "cameras", "status", "preset", "set", "reset", "knobs", "pause", "resume"):
        assert cmd in out
```

- [ ] **Step 2: Run them and expect failures.**
- [ ] **Step 3: Implement** the `settings.py` attributes, `control.py`, `cli.py`, the `pyproject.toml` entry and `mainProgram`.
- [ ] **Step 4: Run the full suite:** green. Then a read-only smoke on westfall: `nix run . -- --help`, `nix run . -- knobs`, `nix run . -- cameras`, and `FLOFICAM_STATE_DIR=$(mktemp -d) nix run . -- set picture.width=320px` (refused). Paste the outputs into the report.
- [ ] **Step 5: Commit.** `feat: floficam command (setup, run, cameras, status, preset, set, reset, knobs, pause, resume)`

### Task 5: VM test and README

**Files:**
- Modify: `nix/vm-test.nix`, `README.md`

**Interfaces:**
- Consumes: everything above, through the installed `floficam` binary.

- [ ] **Step 1: VM test additions.** Add them to the existing `testScript`, after the current subtests. alice gets `extraGroups = [ "video" "wheel" ]`, and the machine gets `security.sudo.wheelNeedsPassword = false`. Run each as alice via `runuser -u alice` with `XDG_RUNTIME_DIR=/run/user/<uid>`:
  - `floficam status` succeeds and contains `running` and `/dev/floficam`.
  - `floficam cameras` prints `no cameras found`: the loopback is skipped for real, and the VM has no other camera.
  - `floficam preset vhs`, then `wait_until_succeeds` on `floficam status` containing `filter=vhs`.
  - `floficam setup --label vmtest` exits 0 through the sudo re-exec path, and `grep -l vmtest /sys/class/video4linux/*/name` matches. Running it again prints `already exists`.
  - The existing bars assertion still passes with the default `camera = "auto"`.
- [ ] **Step 2: Run the VM test** in the foreground: `nix build -L --no-link .#checks.aarch64-linux.vm` should pass. If the machine runs short of memory, report that rather than retrying in a loop.
- [ ] **Step 3: README.**
  - Replace the opening of "Install" with a quick start that works without the modules: `nix run git+https://git.florkiewicz.me/mesh/floficam -- setup`, then `… -- run`, then pick `floficam` in the browser. Follow it with: "Firefox lists it. Chromium-based browsers may not list it until the device is created with exclusive_caps=1 (planned)."
  - Present the modules as the persistent way. Drop `camera = "<node.name>"`, since `auto` is now the default. Say that the home-manager service starts only when `/dev/floficam` and its systemd device unit exist, which needs the udev rule the NixOS module installs. On other distributions, use `floficam run` or add the equivalent rule (quote the rule line from `nix/nixos-module.nix`).
  - Add a "Controlling it" section with the commands and one example each.
  - Replace the `systemctl kill` lines with `floficam pause` / `resume`: the old lines also hit the filter child. Say that pause works while something is streaming.
  - Mention `nix run . -- --help` (the `.` matters).
  - Move the hand-edited JSON into a "Files" subsection.
  - Remove `--camera auto` and the CLI from "Planned next".
- [ ] **Step 4: Final checks.** Run `nix fmt -- --ci`, ruff, `nix develop -c pytest -q`, and `nix build -L --no-link .#checks.aarch64-linux.package .#checks.aarch64-linux.hm-assert`. All green.
- [ ] **Step 5: Commit.** `feat: VM test drives the CLI; README quick start`
