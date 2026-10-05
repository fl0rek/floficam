"""SMPTE bars rendering via GStreamer. The frame it renders at start-up is
installed as the timeout image and seeds the ring (start-up steps 3/5)."""
from ffsb_camera.frames import frame_size, render_bars

WIDTH, HEIGHT = 1280, 720


def test_bars_size():
    assert frame_size(WIDTH, HEIGHT) == 1382400
    assert len(render_bars(WIDTH, HEIGHT)) == 1382400


def test_bars_not_flat():
    frame = render_bars(WIDTH, HEIGHT)
    first_row = frame[:WIDTH]
    last_row = frame[(HEIGHT - 1) * WIDTH:HEIGHT * WIDTH]

    assert len(set(first_row)) >= 6
    assert first_row != last_row  # SMPTE's bottom band differs from its top
