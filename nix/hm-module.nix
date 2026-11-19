# home-manager module: the floficam-daemon user service.
#
# The unit binds to the loopback node's device unit (BindsTo/After on
# `device`): the udev rule from the NixOS module (or the host's own) starts
# it when the node appears, and the binding stops it when the node goes, so
# with no device the start fails on the binding and the unit just stays
# inactive. WantedBy=`target` starts it with the session when the node is
# already there.
{
  lib,
  config,
  pkgs,
  ...
}:
let
  cfg = config.services.floficam;
  inherit (import ./lib.nix { inherit lib; }) systemdDeviceUnit;
  dev = systemdDeviceUnit cfg.device;

  # builtins.match anchors on its own; the message spells the anchors out.
  word = "[A-Za-z0-9._:/@+-]+";
  charset = "^${word}$";
  plain = s: builtins.match word s != null;
  # Every value that lands on the ExecStart line, named for the message.
  argValues = [
    {
      name = "services.floficam.device";
      value = cfg.device;
    }
    {
      name = "services.floficam.camera";
      value = cfg.camera;
    }
  ]
  ++ lib.concatLists (
    lib.mapAttrsToList (k: v: [
      {
        name = "a services.floficam.streamProperties key";
        value = k;
      }
      {
        name = "services.floficam.streamProperties.${builtins.toJSON k}";
        value = v;
      }
    ]) cfg.streamProperties
  );

  args = [
    "--device"
    cfg.device
    "--camera"
    cfg.camera
  ]
  ++ lib.concatLists (
    lib.mapAttrsToList (k: v: [
      "--stream-property"
      "${k}=${v}"
    ]) cfg.streamProperties
  );
in
{
  options.services.floficam = {
    enable = lib.mkEnableOption "floficam-daemon, the v4l2loopback camera feeder";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The floficam package.";
    };

    device = lib.mkOption {
      type = lib.types.str;
      default = "/dev/floficam";
      description = "The v4l2loopback node the daemon writes to (`--device`); the unit binds to its device unit.";
    };

    camera = lib.mkOption {
      type = lib.types.str;
      default = "external";
      description = "`external` (something else links the input) or a PipeWire target-object (`--camera`).";
    };

    streamProperties = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        "media.role" = "Camera";
      };
      description = "PipeWire stream properties, one `--stream-property KEY=VALUE` each, in name order.";
    };

    target = lib.mkOption {
      type = lib.types.str;
      default = "graphical-session.target";
      description = "The user target that starts the unit and that it is part of.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = map (a: {
      assertion = plain a.value;
      message = "${a.name} = ${builtins.toJSON a.value} must match ${charset}";
    }) argValues;

    home.packages = [ cfg.package ];

    systemd.user.services.floficam = {
      Unit = {
        Description = "floficam: feed ${cfg.device} with camera frames, lo-fi effects or SMPTE bars";
        BindsTo = [ dev ];
        # The target here is load-bearing: a target is ordered after every
        # unit it wants unless that unit already orders itself after the
        # target. Without it a missing device (no v4l2loopback for a new
        # kernel) holds the whole session for the device's 90 s job timeout.
        After = [
          dev
          cfg.target
        ];
        PartOf = [ cfg.target ];
      };
      Service = {
        # Unquoted on purpose: the asserted charset excludes %, \, quotes
        # and whitespace, so neither systemd's specifier expansion nor its
        # C-unescaping can touch this line, and each value stays one word.
        ExecStart = lib.concatStringsSep " " ([ "${cfg.package}/bin/floficam-daemon" ] ++ args);
        Restart = "on-failure";
        RestartSec = 2;
        # Exit 78 is the v4l2loopback floor error (the module is too old or
        # misconfigured): permanent until the module is fixed, so restarting
        # every 2 s would only spam the journal.
        RestartPreventExitStatus = 78;
        # Lets the daemon report STATUS="camera=… filter=…" over
        # $NOTIFY_SOCKET without declaring Type=notify: the service's
        # readiness is still the unit starting, not a READY=1 the daemon
        # never sends.
        NotifyAccess = "main";
      };
      Install.WantedBy = [ cfg.target ];
    };
  };
}
