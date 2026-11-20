{
  description = "floficam: v4l2loopback camera feeder with lo-fi effects";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
    in
    {
      nixosModules.default = ./nix/nixos-module.nix;
      homeManagerModules.default = ./nix/hm-module.nix;

      overlays.default = final: prev: {
        floficam = final.callPackage ./nix/package.nix { };
      };

      packages = forAll (
        system: pkgs: rec {
          floficam = pkgs.callPackage ./nix/package.nix { };
          default = floficam;
        }
      );

      checks = forAll (
        system: pkgs:
        let
          inherit (pkgs) lib;
          # Eval-time checks: the verdict is computed while evaluating, the
          # derivation only reports it (and fails the build on a miss).
          verdict =
            name: failures:
            pkgs.runCommand "floficam-${name}" { } (
              if failures == [ ] then
                "touch $out"
              else
                ''
                  printf '%s\n' ${lib.escapeShellArgs failures} >&2
                  exit 1
                ''
            );
        in
        {
          package = self.packages.${system}.floficam;

          unit-name =
            let
              inherit (import ./nix/lib.nix { inherit lib; }) systemdDeviceUnit;
              cases = {
                "/dev/floficam" = "dev-floficam.device";
                "/dev/video10" = "dev-video10.device";
                "/dev/my-cam" = "dev-my\\x2dcam.device";
                "/dev/v4l/by-id/x" = "dev-v4l-by\\x2did-x.device";
              };
            in
            verdict "unit-name" (
              lib.concatLists (
                lib.mapAttrsToList (
                  path: want:
                  let
                    got = systemdDeviceUnit path;
                  in
                  lib.optional (got != want) "systemdDeviceUnit ${path}: got ${got}, want ${want}"
                ) cases
              )
            );

          hm-assert =
            let
              # Evaluated through home-manager's NixOS module, not
              # homeManagerConfiguration: the latter throws on a failed
              # assertion before anything (config included) can be read,
              # while the NixOS module forwards alice's assertions into the
              # system's config.assertions, which reads without throwing.
              system' =
                floficam:
                (nixpkgs.lib.nixosSystem {
                  modules = [
                    home-manager.nixosModules.home-manager
                    {
                      nixpkgs.pkgs = pkgs;
                      boot.loader.grub.enable = false;
                      fileSystems."/" = {
                        device = "none";
                        fsType = "tmpfs";
                      };
                      system.stateVersion = "25.05";
                      users.users.alice.isNormalUser = true;
                      home-manager.users.alice = {
                        imports = [ self.homeManagerModules.default ];
                        home.stateVersion = "25.05";
                        services.floficam = floficam // {
                          enable = true;
                        };
                      };
                    }
                  ];
                }).config;
              failedMessages = config: map (a: a.message) (lib.filter (a: !a.assertion) config.assertions);

              # A value with a space must trip the charset assertion, and its
              # message must name the value.
              bad = failedMessages (system' {
                streamProperties.a = "b c";
              });

              badReserved = failedMessages (system' {
                streamProperties."node.name" = "x";
              });
              badDevice = failedMessages (system' {
                device = "dev/video10";
              });

              # Control: a real deployment's settings pass and give the exact,
              # unquoted ExecStart it expects.
              good = system' {
                device = "/dev/video10";
                camera = "external";
                streamProperties."firefox-sandbox.lane" = "camera-feed";
              };
              alice = good.home-manager.users.alice;
              # home-manager stores ExecStart as a list of command lines.
              goodExec = lib.toList alice.systemd.user.services.floficam.Service.ExecStart;
              wantExec = "${alice.services.floficam.package}/bin/floficam-daemon --device /dev/video10 --camera external --stream-property firefox-sandbox.lane=camera-feed";
            in
            verdict "hm-assert" (
              lib.optional (
                !lib.any (lib.hasInfix "\"b c\"") bad
              ) "no failed assertion names \"b c\"; failed: ${builtins.toJSON bad}"
              ++ lib.optional (
                !lib.any (lib.hasInfix "reserved") badReserved
              ) "reserved key node.name not rejected; failed: ${builtins.toJSON badReserved}"
              ++ lib.optional (
                !lib.any (lib.hasInfix "absolute, normalised") badDevice
              ) "relative device not rejected; failed: ${builtins.toJSON badDevice}"
              ++ lib.optional (
                failedMessages good != [ ]
              ) "control config failed: ${builtins.toJSON (failedMessages good)}"
              ++ lib.optional (
                goodExec != [ wantExec ]
              ) "control ExecStart: got ${builtins.toJSON goodExec}, want ${wantExec}"
            );

          vm = import ./nix/vm-test.nix {
            inherit pkgs self home-manager;
          };
        }
      );

      formatter = forAll (system: pkgs: pkgs.nixfmt-tree);

      apps = forAll (
        system: pkgs:
        let
          floficam = self.packages.${system}.floficam;
          gst = pkgs.gst_all_1;
          render-demos = pkgs.writeShellApplication {
            name = "floficam-render-demos";
            runtimeInputs = [ pkgs.ffmpeg ];
            text = ''
              export FLOFICAM_DEMO_FONT=${pkgs.unscii}/share/fonts/opentype/unscii-16.otf
              export PYTHONPATH=${floficam}/${pkgs.python3.sitePackages}
              export GI_TYPELIB_PATH=${gst.gstreamer.out}/lib/girepository-1.0:${gst.gst-plugins-base}/lib/girepository-1.0
              export GST_PLUGIN_SYSTEM_PATH_1_0=${gst.gstreamer.out}/lib/gstreamer-1.0:${gst.gst-plugins-base}/lib/gstreamer-1.0:${gst.gst-plugins-good}/lib/gstreamer-1.0
              exec ${floficam.pythonEnv}/bin/python ${./tools/render-demos.py} "$@"
            '';
          };
        in
        {
          # Re-renders docs/media from a source clip; see tools/render-demos.py.
          render-demos = {
            type = "app";
            program = "${render-demos}/bin/floficam-render-demos";
            meta.description = "Render the README media through the built-in presets";
          };
        }
      );

      devShells = forAll (
        system: pkgs: {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (p: [
                p.pytest
                p.pygobject3
                p.numpy
              ]))
              pkgs.gst_all_1.gstreamer.out
              pkgs.gst_all_1.gst-plugins-base
              pkgs.gst_all_1.gst-plugins-good
              pkgs.pipewire
              pkgs.gobject-introspection
              pkgs.stdenv.cc
              pkgs.linuxHeaders
            ];
            shellHook = ''
              export GI_TYPELIB_PATH="${pkgs.gst_all_1.gstreamer.out}/lib/girepository-1.0:${pkgs.gst_all_1.gst-plugins-base}/lib/girepository-1.0''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
              export GST_PLUGIN_SYSTEM_PATH_1_0="${pkgs.gst_all_1.gstreamer.out}/lib/gstreamer-1.0:${pkgs.gst_all_1.gst-plugins-base}/lib/gstreamer-1.0:${pkgs.gst_all_1.gst-plugins-good}/lib/gstreamer-1.0:${pkgs.pipewire}/lib/gstreamer-1.0"
              export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
              export C_INCLUDE_PATH="${pkgs.linuxHeaders}/include''${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"
            '';
          };
        }
      );
    };
}
