# Helpers shared by the modules and the flake's checks.
{ lib }:
let
  # Bytes systemd leaves alone in a path-derived unit name; '/' is handled
  # separately (it becomes '-'), and '-' itself is escaped.
  plain = c: builtins.match "[A-Za-z0-9:_.]" c != null;
  hex =
    c: "\\x${lib.fixedWidthString 2 "0" (lib.toLower (lib.toHexString (lib.strings.charToInt c)))}";
  escapeChar =
    c:
    if c == "/" then
      "-"
    else if plain c then
      c
    else
      hex c;
in
{
  # The device unit systemd creates for a /dev path, as `systemd-escape
  # --path --suffix=device` names it: "/dev/my-cam" -> "dev-my\x2dcam.device".
  # Takes an absolute path; ASCII only (charToInt has no entry for other
  # bytes, and the HM module's charset assertion keeps them out anyway).
  systemdDeviceUnit =
    path:
    lib.concatMapStrings escapeChar (lib.stringToCharacters (lib.removePrefix "/" path)) + ".device";
}
