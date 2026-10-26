"""Pure (event, frame age) -> action logic for the camera feeder.

No V4L2, no GStreamer, no clock: `now` is always passed in. See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Running, for the semantics these tests pin.
"""
from ffsb_camera.logic import (
    DEBOUNCE_S,
    FRESH_S,
    Act,
    PauseRefusal,
    State,
    on_frame,
    on_input_error,
    on_pause,
    on_resume,
    on_tick,
    on_usage,
    status,
)


def test_usage_on_starts_input():
    s, acts = on_usage(State(), 1, 0.0)
    assert acts == [Act.REASSERT_TIMEOUT, Act.START_INPUT]
    assert s.input_up is True
    assert s.teardown_at is None


def test_usage_off_debounces():
    s, _ = on_usage(State(), 1, 0.0)
    s, acts = on_usage(s, 0, 0.0)
    assert acts == []
    assert s.teardown_at == DEBOUNCE_S

    # Before the debounce window passes, the input stays up (ticks still
    # write frames, they just don't tear anything down).
    s_before, acts_before = on_tick(s, DEBOUNCE_S - 0.01)
    assert Act.FLUSH not in acts_before
    assert Act.STOP_INPUT not in acts_before

    s_after, acts_after = on_tick(s, DEBOUNCE_S)
    assert acts_after == [Act.FLUSH, Act.STOP_INPUT]
    assert s_after.input_up is False
    assert s_after.teardown_at is None
    assert s_after.last_real is None


def test_flap_within_debounce_keeps_input():
    s, _ = on_usage(State(), 1, 0.0)
    s, _ = on_usage(s, 0, 0.0)
    assert s.teardown_at == DEBOUNCE_S

    s, acts = on_usage(s, 1, 1.0)
    assert acts == []
    assert s.teardown_at is None
    assert s.input_up is True


def test_repeated_usage_on_is_idempotent():
    s, acts = on_usage(State(), 1, 0.0)
    assert acts == [Act.REASSERT_TIMEOUT, Act.START_INPUT]

    s, acts = on_usage(s, 1, 0.5)
    assert acts == []
    assert s.input_up is True


def test_fresh_real_frame_written():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(s, 10.0)
    s, acts = on_tick(s, 10.0 + FRESH_S - 0.01)
    assert acts == [Act.WRITE_REAL]


def test_stale_real_frame_becomes_bars():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(s, 10.0)
    s, acts = on_tick(s, 10.0 + FRESH_S + 0.01)
    assert acts == [Act.WRITE_BARS]


def test_no_frame_yet_writes_bars():
    s, _ = on_usage(State(), 1, 0.0)
    s, acts = on_tick(s, 0.1)
    assert acts == [Act.WRITE_BARS]


def test_idle_writes_nothing():
    s, acts = on_tick(State(), 5.0)
    assert acts == []


def test_input_error_while_used_exits():
    s, _ = on_usage(State(), 1, 0.0)
    s, acts = on_input_error(s)
    assert acts == [Act.EXIT_FAILURE]


def test_input_error_while_idle_stops():
    s, acts = on_input_error(State())
    assert acts == [Act.STOP_INPUT]


def test_input_error_during_debounce_flushes():
    # usage 0 but still inside the debounce window: the input is still
    # up, so the ring may hold real frames and must be flushed before
    # teardown (the ring invariant), same as the debounce-expiry path.
    s, _ = on_usage(State(), 1, 0.0)
    s, _ = on_usage(s, 0, 0.0)
    assert s.input_up is True  # sanity: still up, just debouncing

    s, acts = on_input_error(s)

    assert acts == [Act.FLUSH, Act.STOP_INPUT]
    assert s.input_up is False
    assert s.teardown_at is None
    assert s.last_real is None


def streaming(now=10.0):
    s, _ = on_usage(State(), 1, 0.0)
    return on_frame(s, now)


def test_pause_with_fresh_frame_freezes():
    s, acts, why = on_pause(streaming(10.0), 10.05)
    assert (acts, why, s.paused, s.frozen) == ([Act.FREEZE], None, True, True)
    s = on_frame(s, 10.1)
    assert on_tick(s, 10.15)[1] == [Act.WRITE_FROZEN]
    assert status(s) == "frozen"


def test_paused_no_fresh_frame_writes_bars():          # a lock
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert on_tick(s, 10.0 + FRESH_S + 0.01)[1] == [Act.WRITE_BARS]


def test_paused_card_writes_bars_on_fresh_frames():    # post-restart
    s, _ = on_usage(State(paused=True), 1, 0.0)
    s = on_frame(s, 1.0)
    assert on_tick(s, 1.05)[1] == [Act.WRITE_BARS]
    assert status(s) == "paused-card"
    s, acts, why = on_pause(s, 1.05)
    assert (acts, why, s.frozen) == ([Act.FREEZE], None, True)
    assert on_tick(on_frame(s, 1.1), 1.12)[1] == [Act.WRITE_FROZEN]


def test_pause_refusals():
    assert on_pause(State(), 0.0)[2] is PauseRefusal.INPUT_DOWN
    stale = streaming(10.0)
    s, acts, why = on_pause(stale, 10.0 + FRESH_S + 0.01)   # 10.0+0.2-10.0 < 0.2 in IEEE754
    assert (s, acts, why) == (stale, [], PauseRefusal.NO_FRESH_FRAME)
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s2, acts, why = on_pause(on_frame(s, 10.1), 10.12)
    assert (acts, why, s2.frozen) == ([], PauseRefusal.ALREADY_FROZEN, True)


def test_flap_within_debounce_keeps_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, _ = on_usage(s, 0, 10.1)
    s, _ = on_usage(s, 1, 10.5)
    s = on_frame(s, 10.6)
    assert (s.paused, s.frozen) == (True, True)
    assert on_tick(s, 10.62)[1] == [Act.WRITE_FROZEN]


def test_on_frame_keeps_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert on_frame(s, 10.2).paused is True


def test_teardown_clears_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, _ = on_usage(s, 0, 10.1)
    s, acts = on_tick(s, 10.1 + DEBOUNCE_S)
    assert acts == [Act.FLUSH, Act.STOP_INPUT]
    assert (s.paused, s.frozen) == (False, False)


def test_input_error_usage_0_clears_pause_usage_1_keeps_state():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    kept, acts = on_input_error(s)
    assert (kept, acts) == (s, [Act.EXIT_FAILURE])
    s0, _ = on_usage(s, 0, 10.1)
    s0, _ = on_input_error(s0)
    assert (s0.paused, s0.frozen) == (False, False)


def test_usage_0_with_input_down_clears_loaded_pause():   # stale flag
    s, acts = on_usage(State(paused=True), 0, 0.0)
    assert (acts, s.paused, s.frozen) == ([], False, False)


def test_resume_thaws_then_real():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, acts = on_resume(s)
    assert (acts, s.paused, s.frozen, status(s)) == ([Act.THAW], False, False, "live")
    assert on_tick(on_frame(s, 10.1), 10.12)[1] == [Act.WRITE_REAL]
