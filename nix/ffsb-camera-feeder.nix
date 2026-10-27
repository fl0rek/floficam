# modules/home/ffsb-camera-feeder.nix
#
# ffsb-camera-feeder: owns /dev/video10 (the Firefox sandbox camera
# lane's v4l2loopback stand-in) and writes real camera frames, run through
# its effects child (ffsb-camera-filter, a sibling in the same bin/), SMPTE
# bars, or (while paused) a frame the child holds, or the card, into it.
# Python, PyGObject + GStreamer, packaged like
# firefox-sandbox-sleep (its own directory, pyproject.toml, pytest in
# the check phase — test_v4l2_abi.py's ABI probe needs a C compiler and
# the kernel headers alongside it). Spec:
# docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
# "The feeder", and docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md.
#
# The package: pure logic (ffsb_camera/logic.py), the V4L2 adapter
# (ffsb_camera/v4l2.py), the SMPTE bars renderer (ffsb_camera/frames.py),
# the runtime (ffsb_camera/{pwinput,main}.py: GStreamer input, GLib
# main loop), the pipe to the effects child (ffsb_camera/filterlink.py)
# and the child itself (ffsb_camera/filter/). The unit sets no
# Environment=: the child is found beside the feeder's own entry point
# and reads its settings from $XDG_STATE_HOME/ffsb-camera/. mkFeeder builds it once for both the perSystem package and
# the systemd user service below (same split as
# modules/home/disk-watch.nix).
#
# The service mirrors firefox-sandbox-sleep.nix's pattern (flake.modules.
# homeManager.default, lib.mkIf config.features.firefox.enable), with
# BindsTo/After on dev-video10.device: the udev rule (modules/kmods.nix)
# starts the unit when the loopback node appears and the binding stops it
# when the node goes, so with no device the start fails on the binding and
# the unit just stays inactive (spec: "The feeder").
# After=graphical-session.target keeps the session from waiting on it.
#
# Exposed:
#   - perSystem.packages.ffsb-camera-feeder → `nix build .#ffsb-camera-feeder`
#   - systemd.user.services.ffsb-camera-feeder, under features.firefox.enable
{ ... }:
let
  mkFeeder = pkgs: pkgs.python3Packages.buildPythonApplication {
    pname = "ffsb-camera-feeder";
    version = "0.1.0";
    pyproject = true;
    src = ../../ffsb-camera-feeder;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [ pkgs.python3Packages.pygobject3 pkgs.python3Packages.numpy ];
    buildInputs = [
      # .out: gst_all_1.gstreamer defaults to just the "bin" output
      # (gst-launch/gst-inspect); the typelib (lib/girepository-1.0) and
      # libgstreamer itself live in "out", which is what wrapGAppsHook's
      # GST_PLUGIN_SYSTEM_PATH_1_0/GI_TYPELIB_PATH capture needs to find.
      pkgs.gst_all_1.gstreamer.out
      pkgs.gst_all_1.gst-plugins-base
      pkgs.gst_all_1.gst-plugins-good # jpegenc/jpegdec, vp8enc/vp8dec (codec crush, Task 6)
      pkgs.pipewire # libgstpipewire
    ];
    nativeBuildInputs = [
      pkgs.wrapGAppsNoGuiHook
      pkgs.gobject-introspection
    ];
    nativeCheckInputs = [
      pkgs.python3Packages.pytestCheckHook
      pkgs.stdenv.cc
      pkgs.linuxHeaders
    ];
    meta = {
      description = "Feeds the Firefox sandbox camera lane's v4l2loopback device with real frames or SMPTE bars";
      mainProgram = "ffsb-camera-feeder";
      platforms = pkgs.lib.platforms.linux;
    };
  };
in
{
  config = {
    flake.modules.homeManager.default = { lib, config, pkgs, ... }:
      let
        feeder = mkFeeder pkgs;
      in
      {
        config = lib.mkIf config.features.firefox.enable {
          systemd.user.services.ffsb-camera-feeder = {
            Unit = {
              Description = "Feed /dev/video10 with real camera frames through lo-fi effects, SMPTE bars, or a held pause frame for the Firefox sandbox camera lane";
              BindsTo = [ "dev-video10.device" ];
              # graphical-session.target here is load-bearing: a target is
              # ordered after every unit it wants unless that unit already
              # orders itself after the target. Without it a missing
              # /dev/video10 (no v4l2loopback for a new kernel) holds the
              # whole session -- noctalia, the win2k taskbar -- for the
              # device's 90 s job timeout.
              After = [ "dev-video10.device" "graphical-session.target" ];
              PartOf = [ "graphical-session.target" ];
            };
            Service = {
              ExecStart = "${feeder}/bin/ffsb-camera-feeder";
              Restart = "on-failure";
              RestartSec = 2;
              # Lets the feeder report STATUS="camera=… filter=…" over
              # $NOTIFY_SOCKET (ffsb_camera/notify.py) without declaring
              # Type=notify -- the service's readiness is still the unit
              # starting, not a READY=1 the feeder never sends.
              NotifyAccess = "main";
            };
            Install.WantedBy = [ "graphical-session.target" ];
          };
        };
      };

    perSystem = { pkgs, ... }: {
      packages.ffsb-camera-feeder = mkFeeder pkgs;
    };
  };
}
