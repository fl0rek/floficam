# modules/home/ffsb-camera-feeder.nix
#
# ffsb-camera-feeder: owns /dev/video10 (the Firefox sandbox camera
# lane's v4l2loopback stand-in) and writes either real camera frames or
# SMPTE bars into it. Python, PyGObject + GStreamer, packaged like
# firefox-sandbox-sleep (its own directory, pyproject.toml, pytest in
# the check phase — test_v4l2_abi.py's ABI probe needs a C compiler and
# the kernel headers alongside it). Spec:
# docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
# "The feeder".
#
# This task (3) ships the package only: pure logic (ffsb_camera/logic.py),
# the V4L2 adapter (ffsb_camera/v4l2.py) and the SMPTE bars renderer
# (ffsb_camera/frames.py), all unit-tested. The runtime (GStreamer input,
# main loop, systemd service) is Task 4 — it adds ffsb_camera/main.py and
# a flake.modules.homeManager.default systemd.user.services stanza here,
# reusing mkFeeder below (same split as modules/home/disk-watch.nix).
#
# Exposed:
#   - perSystem.packages.ffsb-camera-feeder → `nix build .#ffsb-camera-feeder`
{ ... }:
let
  mkFeeder = pkgs: pkgs.python3Packages.buildPythonApplication {
    pname = "ffsb-camera-feeder";
    version = "0.1.0";
    pyproject = true;
    src = ../../ffsb-camera-feeder;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [ pkgs.python3Packages.pygobject3 ];
    buildInputs = [
      pkgs.gst_all_1.gstreamer
      pkgs.gst_all_1.gst-plugins-base
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
    perSystem = { pkgs, ... }: {
      packages.ffsb-camera-feeder = mkFeeder pkgs;
    };
  };
}
