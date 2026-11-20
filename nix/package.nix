# floficam: floficam-daemon owns a v4l2loopback device (--device, default
# /dev/floficam) and writes real camera frames, run through its effects
# child (floficam-filter, a sibling in the same bin/), SMPTE bars, or (while
# paused) a frame the child holds, or the card, into it. Python, PyGObject +
# GStreamer, pytest in the
# check phase (test_v4l2_abi.py's ABI probe needs a C compiler and the
# kernel headers alongside it).
{
  lib,
  stdenv,
  python3Packages,
  gst_all_1,
  pipewire,
  wrapGAppsNoGuiHook,
  gobject-introspection,
  linuxHeaders,
}:
python3Packages.buildPythonApplication {
  pname = "floficam";
  version = "0.1.0";
  pyproject = true;
  src = ../.;
  build-system = [ python3Packages.setuptools ];
  dependencies = [
    python3Packages.pygobject3
    python3Packages.numpy
  ];
  buildInputs = [
    # .out: gst_all_1.gstreamer defaults to just the "bin" output
    # (gst-launch/gst-inspect); the typelib (lib/girepository-1.0) and
    # libgstreamer itself live in "out", which is what wrapGAppsHook's
    # GST_PLUGIN_SYSTEM_PATH_1_0/GI_TYPELIB_PATH capture needs to find.
    gst_all_1.gstreamer.out
    gst_all_1.gst-plugins-base
    gst_all_1.gst-plugins-good # jpegenc/jpegdec, vp8enc/vp8dec (codec crush)
    pipewire # libgstpipewire
  ];
  nativeBuildInputs = [
    wrapGAppsNoGuiHook
    gobject-introspection
  ];
  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    stdenv.cc
    linuxHeaders
  ];
  postInstall = ''
    install -Dm644 floficam/filter/presets.json $out/share/floficam/presets.json
  '';
  passthru.pythonEnv = python3Packages.python.withPackages (p: [
    p.pygobject3
    p.numpy
  ]);
  meta = {
    description = "Feeds a v4l2loopback camera device with real frames, effects or SMPTE bars";
    license = lib.licenses.gpl2Plus;
    mainProgram = "floficam";
    platforms = lib.platforms.linux;
  };
}
