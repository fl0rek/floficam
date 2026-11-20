# The modules end to end: the NixOS module's v4l2loopback node + udev rule,
# the home-manager module's user service started both by alice's lingering
# session (target path) and by the udev rule (SYSTEMD_USER_WANTS path), and
# the daemon writing SMPTE bars (camera "auto", no camera found, nothing linked) that a
# capture reader sees bit for bit, before and after a hotplug. Then the
# `floficam` command as alice: status, cameras, preset, and setup through
# its sudo re-exec.
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
      extraGroups = [
        "video"
        "wheel"
      ];
    };
    # `floficam setup` re-runs itself with sudo; no one is there to type a password.
    security.sudo.wheelNeedsPassword = false;

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
      floficam = "${pkg}/bin/floficam"


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

      with subtest("the daemon outlives a reader with no camera"):
          # The reader above has stopped; the input stops after the 2 s
          # debounce. A stop that wedged would exit the daemon 10 s later.
          pid = machine.succeed(f"{user} show -p MainPID --value floficam").strip()
          machine.sleep(16)
          restarts = machine.succeed(f"{user} show -p NRestarts --value floficam").strip()
          assert restarts == "0", f"daemon restarted {restarts} times"
          now = machine.succeed(f"{user} show -p MainPID --value floficam").strip()
          assert now == pid, f"daemon pid changed: {pid} -> {now}"

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

      uid = machine.succeed("id -u alice").strip()


      def cli(args):
          return f"runuser -u alice -- env XDG_RUNTIME_DIR=/run/user/{uid} HOME=/home/alice {floficam} {args}"


      with subtest("floficam status sees the running service"):
          # Waits: an active unit may not have written its runtime file
          # yet. "running (pid", since a bare "running" matches "not running".
          machine.wait_until_succeeds(f"{cli('status')} | grep -F 'running (pid'", timeout=60)
          out = machine.succeed(cli("status"))
          assert "running (pid" in out and "/dev/floficam" in out, out

      with subtest("floficam cameras finds none"):
          # WirePlumber here lists no video devices at all (wpctl status
          # shows none, the loopback included), so this checks the command
          # end to end; tests/test_cameras.py covers skipping the loopback.
          out = machine.succeed(cli("cameras"))
          assert "no cameras found" in out, out

      with subtest("floficam preset reaches the running daemon"):
          machine.succeed(cli("preset vhs"))
          machine.wait_until_succeeds(f"{cli('status')} | grep filter=vhs", timeout=60)

      with subtest("floficam setup creates a device through sudo"):
          machine.succeed(cli("setup --label vmtest"))
          machine.succeed("grep -l vmtest /sys/class/video4linux/*/name")
          out = machine.succeed(cli("setup --label vmtest"))
          assert "already exists" in out, out
    '';
}
