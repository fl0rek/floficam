"""Pure (event, frame age) -> action logic for the camera feeder.

No V4L2, no GStreamer, no clock: `now` is always passed in. See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Running, for the semantics these tests pin, and
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Pause,
re-plumbed", for pause as a hold in the filter child.
"""
from dataclasses import replace

from floficam.logic import (
    DEBOUNCE_S,
    FRESH_S,
    HOLD_DEFAULT_S,
    Act,
    PauseRefusal,
    State,
    on_filter_down,
    on_filter_spawn,
    on_filter_status,
    on_frame,
    on_input,
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
    s = on_frame(s, 10.0, False)
    s, acts = on_tick(s, 10.0 + FRESH_S - 0.01)
    assert acts == [Act.WRITE_REAL]


def test_stale_real_frame_becomes_bars():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(s, 10.0, False)
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


def test_old_input_time_is_stale_even_if_just_arrived():
    # A filtered output is as old as the camera frame it came from: one
    # that took 250 ms through a slow child is stale on arrival.
    up, _ = on_usage(State(), 1, 0.0)
    s = on_frame(up, 10.0 - 0.25, False)
    assert on_tick(s, 10.0)[1] == [Act.WRITE_BARS]


def streaming(now=10.0):
    """Input up, filter ready, one unheld output for an input at `now`."""
    s, _ = on_usage(State(), 1, 0.0)
    s = on_filter_status(s, True)
    s = on_input(s, now)
    return on_frame(s, now, False)


def test_pause_with_fresh_frame_grabs():
    s, acts, why = on_pause(streaming(10.0), 10.05)
    assert (acts, why, s.paused, s.frozen) == ([Act.HOLD_GRAB], None, True, True)
    s = on_frame(s, 10.1, True)
    assert on_tick(s, 10.15)[1] == [Act.WRITE_REAL]
    assert status(s) == "frozen"


def test_paused_no_fresh_frame_writes_bars():          # a lock
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert on_tick(s, 10.0 + FRESH_S + 0.01)[1] == [Act.WRITE_BARS]
    s = on_frame(s, 10.06, True)
    assert on_tick(s, 10.06 + FRESH_S + 0.01)[1] == [Act.WRITE_BARS]


def test_paused_card_writes_bars_on_fresh_frames():    # post-restart
    s, _ = on_usage(State(paused=True), 1, 0.0)
    s = on_filter_status(s, True)
    s = on_input(s, 1.0)
    s = on_frame(s, 1.0, True)
    assert on_tick(s, 1.05)[1] == [Act.WRITE_BARS]
    assert status(s) == "paused-card"
    s, acts, why = on_pause(s, 1.05)
    assert (acts, why, s.frozen) == ([Act.HOLD_GRAB], None, True)
    assert on_tick(on_frame(s, 1.1, True), 1.12)[1] == [Act.WRITE_REAL]


def test_pause_refusals():
    assert on_pause(State(), 0.0)[2] is PauseRefusal.INPUT_DOWN
    stale = streaming(10.0)
    s, acts, why = on_pause(stale, 10.0 + FRESH_S + 0.01)   # 10.0+0.2-10.0 < 0.2 in IEEE754
    assert (s, acts, why) == (stale, [], PauseRefusal.NO_FRESH_FRAME)
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s = on_input(on_frame(s, 10.1, True), 10.1)
    s2, acts, why = on_pause(s, 10.12)
    assert (s2, acts, why) == (s, [], PauseRefusal.ALREADY_FROZEN)


def test_pause_refused_filter_not_ready():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(on_input(s, 10.0), 10.0, False)
    s2, acts, why = on_pause(s, 10.05)
    assert (s2, acts, why) == (s, [], PauseRefusal.FILTER_NOT_READY)
    s = on_filter_status(streaming(10.0), False)            # filter in error
    s2, acts, why = on_pause(s, 10.05)
    assert (s2, acts, why) == (s, [], PauseRefusal.FILTER_NOT_READY)


def test_pause_freshness_uses_last_input():
    # The child is silent (HOLD_EMPTY after a restart, say): no output
    # yet, but the camera is flowing, so a pause is still accepted.
    s, _ = on_usage(State(), 1, 0.0)
    s = on_input(on_filter_status(s, True), 10.0)
    assert s.last_real is None
    s2, acts, why = on_pause(s, 10.05)
    assert (acts, why, s2.frozen, s2.bridge_t) == ([Act.HOLD_GRAB], None, True, None)
    # And a fresh output with a stale input time cannot happen, but a
    # stale last_input refuses whatever last_real says.
    s3 = replace(streaming(10.0), last_input=9.0)
    assert on_pause(s3, 10.05)[2] is PauseRefusal.NO_FRESH_FRAME


def test_paused_frozen_ignores_unheld_outputs():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s = on_frame(s, 10.06, True)
    s = on_frame(s, 10.3, False)                            # child ignored the hold
    assert (s.last_real, s.last_real_held) == (10.06, True)
    assert on_tick(s, 10.3)[1] == [Act.WRITE_BARS]
    s = on_frame(s, 10.31, True)
    assert on_tick(s, 10.32)[1] == [Act.WRITE_REAL]


def test_paused_not_frozen_always_bars():
    s, _ = on_usage(State(paused=True), 1, 0.0)
    s = on_filter_status(on_input(s, 1.0), True)
    for t, held in [(1.0, False), (1.03, True), (1.06, False), (1.09, True)]:
        s = on_frame(s, t, held)
        assert on_tick(s, t + 0.01)[1] == [Act.WRITE_BARS]


def test_bridge_until_first_held():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert (s.pause_t, s.bridge_t, s.held_seen) == (10.05, 10.0, False)
    assert on_tick(s, 10.08)[1] == [Act.WRITE_BRIDGE]
    s = on_frame(s, 10.07, False)                           # in flight at the press
    assert on_tick(s, 10.1)[1] == [Act.WRITE_BRIDGE]
    s = on_frame(s, 10.09, True)
    assert on_tick(s, 10.12)[1] == [Act.WRITE_REAL]
    # A held output that goes stale while the bridge frame would still
    # be fresh: bars, never the bridge again.
    s = on_frame(s, 9.9, True)
    assert on_tick(s, 10.12)[1] == [Act.WRITE_BARS]
    s = on_frame(s, 10.13, True)
    for t in (10.14, 10.17, 10.2):
        assert on_tick(s, t)[1] != [Act.WRITE_BRIDGE]


def test_bridge_never_newer_than_press():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert s.bridge_t <= s.pause_t
    s2 = on_frame(s, 10.1, False)                           # a post-press unheld output
    assert (s2.bridge_t, s2.last_real) == (10.0, 10.0)
    # Even a state with bridge_t > pause_t (unreachable) never bridges.
    assert on_tick(replace(s, bridge_t=10.06), 10.07)[1] == [Act.WRITE_BARS]


def test_bridge_none_when_last_output_held():
    # The last output before the press was itself a held frame (an old
    # hold still draining after a resume): no bridge, bars until the new
    # held frame.
    s, _ = on_resume(on_frame(on_pause(streaming(10.0), 10.05)[0], 10.06, True))
    s = on_input(s, 10.07)
    s, acts, _ = on_pause(s, 10.08)
    assert (acts, s.bridge_t) == ([Act.HOLD_GRAB], None)
    assert on_tick(s, 10.09)[1] == [Act.WRITE_BARS]


def test_bridge_expires_after_fresh_s():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert on_tick(s, 10.1)[1] == [Act.WRITE_BRIDGE]
    assert on_tick(s, 10.05 + 0.25)[1] == [Act.WRITE_BARS]


def test_filter_down_clears_frozen_and_refuses_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s = on_input(on_frame(s, 10.06, True), 10.07)
    s = on_filter_down(s)
    assert (s.paused, s.frozen, s.filter_ready) == (True, False, False)
    assert status(s) == "paused-card"
    assert on_tick(s, 10.08)[1] == [Act.WRITE_BARS]
    s2, acts, why = on_pause(s, 10.08)
    assert (s2, acts, why) == (s, [], PauseRefusal.FILTER_NOT_READY)


def test_status_not_ok_while_frozen_is_paused_card():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s = on_frame(s, 10.06, True)
    s = on_filter_status(s, False)
    assert (s.paused, s.frozen, s.filter_ready, status(s)) == (True, False, False, "paused-card")
    assert on_tick(on_frame(s, 10.07, True), 10.08)[1] == [Act.WRITE_BARS]
    # Recovery never auto-grabs: still paused-card.
    s = on_filter_status(s, True)
    assert (s.frozen, s.filter_ready, status(s)) == (False, True, "paused-card")


def test_spawn_while_paused_sends_hold_empty():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, acts = on_filter_spawn(on_frame(s, 10.06, True))
    assert (acts, s.paused, s.frozen, status(s)) == ([Act.HOLD_EMPTY], True, False, "paused-card")
    s, acts = on_filter_spawn(streaming(10.0))
    assert (acts, s.paused, s.frozen) == ([], False, False)


def test_flap_within_debounce_keeps_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, _ = on_usage(s, 0, 10.1)
    s, _ = on_usage(s, 1, 10.5)
    s = on_frame(s, 10.6, True)
    assert (s.paused, s.frozen) == (True, True)
    assert on_tick(s, 10.62)[1] == [Act.WRITE_REAL]


def test_on_frame_keeps_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert on_frame(s, 10.2, True).paused is True
    assert on_frame(s, 10.2, False).paused is True


def test_teardown_clears_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, _ = on_usage(s, 0, 10.1)
    s, acts = on_tick(s, 10.1 + DEBOUNCE_S)
    assert acts == [Act.FLUSH, Act.STOP_INPUT, Act.RELEASE]
    assert (s.paused, s.frozen) == (False, False)


def test_input_error_usage_0_clears_pause_usage_1_keeps_state():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    kept, acts = on_input_error(s)
    assert (kept, acts) == (s, [Act.EXIT_FAILURE])
    s0, _ = on_usage(s, 0, 10.1)
    s0, acts = on_input_error(s0)
    assert acts == [Act.FLUSH, Act.STOP_INPUT, Act.RELEASE]
    assert (s0.paused, s0.frozen) == (False, False)


def test_usage_0_with_input_down_clears_loaded_pause():   # stale flag
    s, acts = on_usage(State(paused=True), 0, 0.0)
    assert (acts, s.paused, s.frozen) == ([Act.RELEASE], False, False)


def test_resume_releases_then_real():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s, acts = on_resume(on_frame(s, 10.06, True))
    assert (acts, s.paused, s.frozen, status(s)) == ([Act.RELEASE], False, False, "live")
    assert on_tick(on_frame(s, 10.1, False), 10.12)[1] == [Act.WRITE_REAL]


def test_every_unpause_path_releases():
    paused, _, _ = on_pause(streaming(10.0), 10.05)
    debouncing, _ = on_usage(paused, 0, 10.1)
    loaded = State(paused=True)                            # flag file, input down
    paths = {
        "resume": on_resume(paused),
        "teardown": on_tick(debouncing, 10.1 + DEBOUNCE_S),
        "input error, usage 0": on_input_error(debouncing),
        "input error, input down": on_input_error(loaded),
        "stale flag": on_usage(loaded, 0, 0.0),
    }
    for name, (s, acts) in paths.items():
        assert (s.paused, s.frozen, acts[-1]) == (False, False, Act.RELEASE), name
    # Nothing to release when nothing was paused.
    assert Act.RELEASE not in on_tick(on_usage(streaming(10.0), 0, 10.1)[0], 10.1 + DEBOUNCE_S)[1]


# Hold: the last frame stays up to hold_s through a gap, then bars.

def ready(s):
    return on_filter_status(s, True)


def test_hold_keeps_writing_the_last_frame():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 10.0, False)
    assert on_tick(s, 15.0, hold_s=10.0)[1] == [Act.WRITE_REAL]
    assert on_tick(s, 20.01, hold_s=10.0)[1] == [Act.WRITE_BARS]


def test_hold_zero_is_fresh_s():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 10.0, False)
    assert on_tick(s, 10.0 + FRESH_S - 0.01, hold_s=0.0)[1] == [Act.WRITE_REAL]
    assert on_tick(s, 10.0 + FRESH_S + 0.01, hold_s=0.0)[1] == [Act.WRITE_BARS]


def test_hold_inf_never_bars_while_up():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 10.0, False)
    assert on_tick(s, 1e9, hold_s=float("inf"))[1] == [Act.WRITE_REAL]


def test_broken_settings_still_bar_under_hold():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 10.0, False)
    s = on_filter_status(s, False)
    assert on_tick(s, 11.0, hold_s=10.0)[1] == [Act.WRITE_BARS]


def test_filter_down_still_bars_under_hold():
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 10.0, False)
    s = on_filter_down(s)
    assert on_tick(s, 11.0, hold_s=10.0)[1] == [Act.WRITE_BARS]


def test_hold_rearms_on_a_flap_inside_debounce():
    # A new reader inside the debounce keeps the input up and last_real,
    # but must not see the previous reader's last frame.
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 1.0, False)
    s, _ = on_usage(s, 0, 2.0)
    s, acts = on_usage(s, 1, 2.5)
    assert Act.STOP_INPUT not in acts
    assert on_tick(s, 2.6, hold_s=float("inf"))[1] == [Act.WRITE_BARS]
    s = on_frame(s, 2.6, False)
    assert on_tick(s, 5.0, hold_s=float("inf"))[1] == [Act.WRITE_REAL]


def test_hold_applies_to_a_frozen_pause():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    s = on_frame(s, 10.0, True)
    assert on_tick(s, 15.0, hold_s=10.0)[1] == [Act.WRITE_REAL]
    assert on_tick(s, 20.01, hold_s=10.0)[1] == [Act.WRITE_BARS]


def test_hold_extends_the_bridge_not_paused_card():
    s, _, _ = on_pause(streaming(10.0), 10.05)
    assert (s.bridge_t, s.held_seen) == (10.0, False)
    assert on_tick(s, 15.0, hold_s=10.0)[1] == [Act.WRITE_BRIDGE]
    assert on_tick(s, 20.01, hold_s=10.0)[1] == [Act.WRITE_BARS]
    # hold 0: the bridge lasts FRESH_S, as before.
    assert on_tick(s, 10.0 + FRESH_S - 0.01, hold_s=0.0)[1] == [Act.WRITE_BRIDGE]
    assert on_tick(s, 10.0 + FRESH_S + 0.01, hold_s=0.0)[1] == [Act.WRITE_BARS]
    # paused-card (a respawned child holds nothing): bars at once.
    card, _ = on_filter_spawn(s)
    assert (card.paused, card.frozen) == (True, False)
    assert on_tick(card, 10.06, hold_s=10.0)[1] == [Act.WRITE_BARS]


def test_hold_default_is_ten():
    assert HOLD_DEFAULT_S == 10.0


def test_hold_disarms_until_new_output_after_filter_recovers():
    # A respawned child (or fixed settings) says ok before any output:
    # the pre-crash frame must not come back after the bars.
    s, _ = on_usage(State(), 1, 0.0)
    s = on_frame(ready(s), 10.0, False)
    down, _ = on_filter_spawn(on_filter_down(s))
    broken = on_filter_status(s, False)
    for back in (ready(down), ready(broken)):
        assert on_tick(back, 12.0, hold_s=10.0)[1] == [Act.WRITE_BARS]
        back = on_frame(back, 12.0, False)
        assert on_tick(back, 15.0, hold_s=10.0)[1] == [Act.WRITE_REAL]
