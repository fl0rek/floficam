{
  description = "floficam: v4l2loopback camera feeder with lo-fi effects";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
    in
    {
      overlays.default = final: prev: {
        floficam = final.callPackage ./nix/package.nix { };
      };

      packages = forAll (
        system: pkgs: rec {
          floficam = pkgs.callPackage ./nix/package.nix { };
          default = floficam;
        }
      );

      checks = forAll (system: pkgs: {
        package = self.packages.${system}.floficam;
      });

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
