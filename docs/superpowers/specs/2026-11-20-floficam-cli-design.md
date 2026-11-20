# floficam CLI and imperative setup: design

Approved by florek 2026-11-20 ("yup") after trying the repo cold: the only
entry point was `floficam-daemon`, its `--help` described internals, and with
no `/dev/floficam` it died with `could not open /dev/floficam` and no hint.
This pulls the CLI of the P2 spec (monorepo
`docs/superpowers/specs/2026-11-19-floficam-extraction-design.md`, "CLI")
forward, adds an imperative `setup` and `run` for people not using the
modules, and adds `--camera auto`. P2's preview, file/picture sources, tray
and idle bars stay in P2.

## Goals

- `floficam` with no arguments, or `-h`, tells a newcomer what to type.
- Someone with no NixOS module and no service gets a working virtual camera
  with `floficam setup` then `floficam run`.
- Runtime control without hand-editing JSON: switch presets, tweak knobs,
  pause, see status. Works the same whether the daemon runs as the user
  service or as `floficam run`.
- A missing device names the fix.

## Non-goals

- Preview window, sources, tray, idle bars, `exclusive_caps=1` (P2).
- Changing the westfall lane: it sets `camera = "external"` and
  `device = "/dev/video10"` explicitly, so new defaults don't reach it.

## Commands

```
floficam                     overview with examples (same as -h)
floficam setup               create the virtual camera (sudo), idempotent
floficam run                 run the feeder in the foreground
floficam cameras             cameras PipeWire offers, virtual ones skipped
floficam status              daemon state, device, camera, preset, errors
floficam preset [ID]         list presets (active one marked) / select one
floficam set KNOB=VALUE...   overrides on top of the active preset
floficam reset               drop overrides
floficam knobs               every knob: range or choices, default, current
floficam pause | resume      freeze / unfreeze
```

Every command has user-facing `-h`. Exact wording and output layout are
the builder's.

## Behaviour

- **Device lookup.** The device is the `--device` path if given. Otherwise
  `/dev/floficam` if it exists, else the `/dev/videoN` whose sysfs `name`
  is `floficam` (the card label). This applies to the daemon and to every
  command that reports the device. With none found the daemon exits 1
  with: no floficam device; create one with `floficam setup` (or
  `programs.floficam.loopback.enable` on NixOS).
- **`setup`.** Exits 0 at once if a device named `floficam` exists, saying
  which. Otherwise it needs root; when not root it re-runs itself under
  `sudo` (the same executable, so a Nix store wrapper keeps its
  environment) and says so. As root: if `/dev/v4l2loopback` (the control
  device) exists, the module is loaded and it adds a device with
  `V4L2LOOPBACK_CTL_ADD` (`card_label=floficam`, `max_buffers=2`,
  `announce_all_caps=1` i.e. `exclusive_caps=0`, everything else -1/0
  as the header documents). Else `modprobe v4l2loopback devices=1
  card_label=floficam exclusive_caps=0 max_buffers=2`. If modprobe
  fails because the module is not installed, it says how to get it
  (distro package; `programs.floficam.loopback.enable` on NixOS). It
  prints what it did and the resulting `/dev/videoN`. The device lasts
  until reboot or module unload; the modules remain the persistent way.
- **`run`.** The daemon in the foreground, `--camera auto` by default,
  same flags as `floficam-daemon`.
- **`--camera auto`.** The daemon picks a camera at every input start
  through `Gst.DeviceMonitor` (`Video/Source`), skipping any device whose
  `api.v4l2.cap.driver` is `v4l2 loopback` or whose `api.v4l2.path` (or
  `device.path`) resolves to the output device. First remaining one wins,
  by its `node.name` (falling back to `object.serial`). Only hardware cameras (`device.api` v4l2 or libcamera) are candidates: a
  screencast or other virtual `Video/Source` is never picked. The lookup
  runs off the main thread, in the input's start job. None: the input is
  left unlinked (bars), logged once per start. `cameras` lists the same
  set and marks what `auto` would pick. The home-manager module's default
  becomes `auto`.
- **Runtime file.** The daemon writes `$XDG_RUNTIME_DIR/floficam/daemon.json`
  (`pid`, `start_time`, `device`, `camera`, `status`: the StatusText line,
  `signal_seq`, `pause_refused`), atomically,
  on start and on every status change, and removes it on exit. `status`,
  `pause` and `resume` use it, so they work for `run` too. A file whose
  pid is gone is stale and reported as not running.
- **Pause.** `pause`/`resume` send SIGUSR1/SIGUSR2 to the daemon pid only. They wait
  for the daemon to confirm and report a refusal (for example nothing is
  streaming) with exit 1.
  The filter child shares the service's cgroup and dies on SIGUSR1, so a
  plain `systemctl --user kill -s USR1 floficam` (as the README said) is
  wrong; the README moves to `floficam pause`.
- **Settings writes.** `preset`, `set`, `reset` write `filter.json`
  atomically (temp file + rename in the same dir), validate with
  `filter/settings.py` first and refuse with the child's message on a bad
  knob or unknown preset, so a typo never turns the camera into bars.
  They work with no daemon running (applied at next start).
- **Packaging.** `floficam` is the package's `mainProgram`, so
  `nix run <flake> -- setup` works. `floficam-daemon` stays, and the
  service keeps calling it. Daemon `--help` loses its spec references.

## Testing

- Unit: device lookup over a fake sysfs tree; `auto` selection over fake
  device lists (loopback skipped, own output skipped, none → unlinked);
  each settings verb (atomic write, refusal leaves the file untouched);
  pause/resume/status against a fake runtime file and a live child
  process; setup's decision (exists / ctl device / modprobe / not root)
  with the privileged calls injected; the ioctl struct packing against
  the header layout above.
- VM test: `floficam setup` as root with the module loaded adds a second
  device (a hidden `--label` option names it), proving the ioctl on a real
  kernel; `floficam status` and `floficam preset vhs` against the running
  service.
