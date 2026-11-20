# The modules end to end: the NixOS module's v4l2loopback node + udev rule,
# the home-manager module's user service started both by alice's lingering
# session (target path) and by the udev rule (SYSTEMD_USER_WANTS path), and
# the daemon writing SMPTE bars (camera "auto", no camera found, nothing linked) that a
# capture reader sees bit for bit, before and after a hotplug.
{
  pkgs,
  self,
  home-manager,
}:
pkgs.testers.runNixOSTest {
  name = "floficam";

  nodes.machine = {
    imports = [
      self.nixosModules.default
      home-manager.nixosModules.home-manager
    ];

    programs.floficam.loopback.enable = true;
    services.pipewire.enable = true; # WirePlumber is on by default

    users.users.alice = {
      isNormalUser = true;
      linger = true;
      extraGroups = [ "video" ];
    };

    home-manager = {
      useGlobalPkgs = true;
      useUserPackages = true;
      users.alice = {
        imports = [ self.homeManagerModules.default ];
        home.stateVersion = "25.05";
        services.floficam = {
          enable = true;
          # No graphical session in the VM; lingering starts default.target.
          target = "default.target";
        };
      };
    };
  };

  testScript =
    { nodes, ... }:
    let
      pkg = nodes.machine.home-manager.users.alice.services.floficam.package;
      inherit (pkg.passthru) pythonEnv;
      gst = pkgs.gst_all_1;
      renderBars = pkgs.writeShellScript "render-bars" ''
        export PYTHONPATH=${pkg}/${pythonEnv.sitePackages}
        export GI_TYPELIB_PATH=${gst.gstreamer.out}/lib/girepository-1.0:${gst.gst-plugins-base}/lib/girepository-1.0
        export GST_PLUGIN_SYSTEM_PATH_1_0=${gst.gstreamer.out}/lib/gstreamer-1.0:${gst.gst-plugins-base}/lib/gstreamer-1.0
        exec ${pythonEnv}/bin/python3 -c 'import sys; from floficam.frames import render_bars; sys.stdout.buffer.write(render_bars(1280, 720))'
      '';
    in
    ''
      user = "systemctl --user --machine=alice@"
      reader = "${pkg}/bin/floficam-reader"


      def crcs(output):
          return [line.split()[-1] for line in output.splitlines() if line.startswith("frame ")]


      def wait_active():
          machine.wait_until_succeeds(f"{user} is-active floficam")


      def assert_bars(want):
          # The unit is active once the daemon's process starts, before it has
          # set the fresh node's format, and a capture STREAMON on a node no
          # writer has configured fails with EIO: retry until the read gets
          # through. --seconds bounds a stalled device: fewer than 10 lines
          # fails below.
          out = machine.wait_until_succeeds(
              f"runuser -u alice -- {reader} --device /dev/floficam --frames 10 --seconds 30",
              timeout=120,
          )
          got = crcs(out)
          assert got == [want] * 10, f"want 10 bars frames ({want}), got {got}"


      machine.wait_for_unit("multi-user.target")
      machine.wait_until_succeeds("test -L /dev/floficam")

      with subtest("lingering session starts the unit (target path)"):
          wait_active()

      with subtest("the device carries SMPTE bars"):
          machine.succeed("${renderBars} > /tmp/bars.nv12")
          (bars,) = crcs(machine.succeed(f"{reader} --file /tmp/bars.nv12"))
          assert_bars(bars)

      with subtest("udev remove/add restarts the unit (SYSTEMD_USER_WANTS path)"):
          machine.succeed(f"{user} stop floficam")
          machine.succeed("udevadm trigger --settle --action=remove --subsystem-match=video4linux --attr-match=name=floficam")
          machine.succeed("udevadm trigger --settle --action=add --subsystem-match=video4linux --attr-match=name=floficam")
          wait_active()

      with subtest("module reload restarts the unit (real hotplug)"):
          machine.succeed(f"{user} stop floficam")
          # WirePlumber's v4l2 monitor may hold the node for a moment.
          machine.wait_until_succeeds("modprobe -r v4l2loopback")
          machine.succeed("modprobe v4l2loopback")
          machine.wait_until_succeeds("test -L /dev/floficam")
          wait_active()
          assert_bars(bars)
    '';
}
