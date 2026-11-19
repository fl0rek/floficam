"""`floficam-reader --file`: the same crc the device path reports, measured by
the same code, so a reference file and a live frame compare directly."""
import zlib

from floficam import reader
from floficam.frames import render_bars


def test_file_mode_matches_bars(tmp_path, capsys):
    bars = render_bars(1280, 720)
    path = tmp_path / "bars.nv12"
    path.write_bytes(bars)
    assert reader.main(["--file", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    parts = lines[0].split()
    assert parts[0] == "frame"
    assert parts[-1] == f"{zlib.crc32(bars[:1280 * 720]):08x}"
