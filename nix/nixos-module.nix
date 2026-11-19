# NixOS module: the v4l2loopback node floficam-daemon writes to.
#
# Loads v4l2loopback with one device at /dev/video<videoNr>, named `label`,
# and tags it for systemd so the user service (the home-manager module) can
# bind to it and be started by it.
{ lib, config, ... }:
let
  cfg = config.programs.floficam.loopback;
  v4l2loopback = config.boot.kernelPackages.v4l2loopback;
in
{
  options.programs.floficam.loopback = {
    enable = lib.mkEnableOption "the v4l2loopback node floficam feeds";

    videoNr = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 10;
      description = "The node's number: /dev/video<videoNr>.";
    };

    label = lib.mkOption {
      type = lib.types.str;
      default = "floficam";
      description = "The node's card label, which is also what the udev rule matches on.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.versionAtLeast v4l2loopback.version "0.15.4";
        message = "programs.floficam.loopback needs v4l2loopback >= 0.15.4, this kernel's is ${v4l2loopback.version}";
      }
    ];

    boot.extraModulePackages = [ v4l2loopback ];
    boot.kernelModules = [ "v4l2loopback" ];
    # exclusive_caps=0 is P1-interim: the node advertises output and
    # capture at once. P2 flips it together with idle bars.
    boot.extraModprobeConfig = ''
      options v4l2loopback devices=1 video_nr=${toString cfg.videoNr} card_label="${cfg.label}" exclusive_caps=0 max_buffers=2
    '';

    # systemd's stock udev rules don't tag video4linux devices, so without
    # this there would be no device unit for the user service to bind to,
    # and nothing to start it when the node appears. The symlink gives the
    # node a stable name (/dev/floficam, the HM module's default) whatever
    # its number.
    services.udev.extraRules = ''
      SUBSYSTEM=="video4linux", ATTR{name}=="${cfg.label}", TAG+="systemd", ENV{SYSTEMD_USER_WANTS}="floficam.service", SYMLINK+="floficam"
    '';
  };
}
