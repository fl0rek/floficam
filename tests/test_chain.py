"""The hold-capable chain skeleton: identity pixels, real hold modes.
Tasks 5-7 add the effect stages and the fps/stall gate on top of this.
See docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"Effect chain", stage (1), and "The held flag".
"""
import random

from ffsb_camera.filter.chain import Chain, Output
from ffsb_camera.filter.settings import Analog, Knobs

A = b"A" * 100
B = b"B" * 100
C = b"C" * 100


def rng(seed):
    return random.Random(seed)


def test_identity_without_hold():
    c = Chain(Knobs(), rng(0))
    assert c.process(A, 0) == Output(A, False)
    assert c.holding == "none"


def test_hold_grab_holds_next_input_and_flags():
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.hold_grab()
    o1 = c.process(B, 1)
    o2 = c.process(C, 2)
    assert o1 == o2 == Output(B, True)
    assert c.holding == "frame"


def test_hold_empty_emits_nothing():
    c = Chain(Knobs(), rng(0))
    c.hold_empty()
    assert c.process(A, 0) is None
    assert c.holding == "empty"


def test_release_back_to_live():
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.hold_grab()
    c.process(B, 1)
    c.release()
    assert c.holding == "none"
    assert c.process(C, 3) == Output(C, False)


def test_set_knobs_keeps_hold():
    c = Chain(Knobs(), rng(0))
    c.hold_grab()
    c.process(B, 1)
    c.set_knobs(Knobs(analog=Analog(grain=0.0)))
    assert c.process(C, 2) == Output(B, True)
    assert c.holding == "frame"


def test_reset_drops_hold():
    c = Chain(Knobs(), rng(0))
    c.hold_grab()
    c.process(B, 1)
    c.reset()
    assert c.holding == "none"
    assert c.process(C, 2) == Output(C, False)


def test_close_is_idempotent():
    c = Chain(Knobs(), rng(0))
    c.process(A, 0)
    c.close()
    c.close()
