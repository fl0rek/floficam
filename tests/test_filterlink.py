"""FilterLink, the feeder's side of the pipe to the filter child: epochs,
the one-frame slot and its freshness drop, control ordering, respawn,
backoff and the hang watchdog. Runs against `fake_child.py` (real
subprocesses, real pipes), with a fake clock and a recording sleep where
timing matters. Every wait is bounded, so a bug fails instead of hanging.
See docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md,
"The feeder" > "Child management".
"""
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ffsb_camera.filter.protocol import (
    FRAME,
    HOLD_EMPTY,
    HOLD_GRAB,
    RESET,
    STATUS,
    Parser,
    decode_frame_in,
    decode_reset,
    encode_frame_out,
)
from ffsb_camera.filterlink import FilterLink

FAKE = Path(__file__).with_name("fake_child.py")
BIG = b"\x10" * (1280 * 720 * 3 // 2)  # one NV12 frame: larger than a pipe buffer
SMALL_A = b"A" * 64
SMALL_B = b"B" * 64
TIMEOUT_S = 5.0


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class Harness:
    """A FilterLink over fake children. `envs[n]` is the extra env of the
    n-th spawn (the last one repeats); each child records its stdin to
    `rec-<n>.bin`. The sleep records its argument into `events` and
    returns at once, until `max_sleeps` calls, after which it blocks until
    teardown, so a respawn loop can't run away."""

    def __init__(self, tmp_path, envs=({},), max_sleeps=10,
                 argv=(sys.executable, str(FAKE)), **kw):
        self.tmp_path = tmp_path
        self.envs = list(envs)
        self.max_sleeps = max_sleeps
        self.cv = threading.Condition()
        self.procs = []
        self.records = []
        self.outputs = []
        self.ages = []  # clock() - input_t of each output, at delivery (real clock)
        self.statuses = []
        self.events = []
        self.sleeps = []
        self.on_spawn_hook = None
        self.sleep_gate = None  # an Event: each sleep waits for it
        self._closed = threading.Event()
        self.link = FilterLink(
            list(argv),
            on_output=self._on_output,
            on_status=self._on_status,
            on_spawn=self._on_spawn,
            on_down=self._on_down,
            sleep=self._sleep,
            popen=self._popen,
            **kw,
        )

    def _popen(self, argv, **kw):
        with self.cv:
            n = len(self.procs)
        env = dict(os.environ)
        env.update(self.envs[min(n, len(self.envs) - 1)])
        rec = self.tmp_path / f"rec-{n}.bin"
        env["FAKE_RECORD"] = str(rec)
        p = subprocess.Popen(argv, env=env, **kw)
        with self.cv:
            self.procs.append(p)
            self.records.append(rec)
            self.cv.notify_all()
        return p

    def _note(self, event):
        with self.cv:
            self.events.append(event)
            self.cv.notify_all()

    def _on_output(self, frame, input_t, held):
        with self.cv:
            self.outputs.append((frame, input_t, held))
            self.ages.append(time.monotonic() - input_t)
            self.cv.notify_all()

    def _on_status(self, d):
        with self.cv:
            self.statuses.append(d)
            self.cv.notify_all()

    def _on_spawn(self):
        self._note("spawn")
        if self.on_spawn_hook is not None:
            self.on_spawn_hook()

    def _on_down(self):
        self._note("down")

    def _sleep(self, s):
        with self.cv:
            self.sleeps.append(s)
            self.events.append(("sleep", s))
            self.cv.notify_all()
            blocked = len(self.sleeps) >= self.max_sleeps
        if blocked:
            self._closed.wait(TIMEOUT_S * 4)
        elif self.sleep_gate is not None:
            self.sleep_gate.wait(TIMEOUT_S)

    def wait_for(self, pred, timeout=TIMEOUT_S):
        with self.cv:
            assert self.cv.wait_for(pred, timeout), "timed out"

    def received(self, n):
        """Messages the n-th child has read so far."""
        try:
            data = self.records[n].read_bytes()
        except FileNotFoundError:
            return []
        return Parser().feed(data)

    def wait_received(self, n, pred, timeout=TIMEOUT_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self.received(n)
            if pred(msgs):
                return msgs
            time.sleep(0.01)
        raise AssertionError(f"child {n} received {[k for k, _ in self.received(n)]}")

    def frames_received(self, n):
        return [decode_frame_in(p) for k, p in self.received(n) if k == FRAME]

    def spawned(self, n):
        return lambda: len(self.procs) >= n and self.link.up

    def close(self):
        self._closed.set()
        self.link.stop()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(TIMEOUT_S)


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(**kw):
        h = Harness(tmp_path, **kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.close()


def kinds(msgs):
    return [k for k, _ in msgs]


def test_echo_roundtrip_carries_input_time(make):
    h = make()
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    t0 = time.monotonic()
    h.link.submit(SMALL_A, t0, 1)
    h.wait_for(lambda: h.outputs)
    frame, input_t, held = h.outputs[0]
    assert frame == SMALL_A
    assert input_t == round(t0 * 1e9) / 1e9
    assert held is False
    assert h.link.last_input_t == t0
    h.wait_for(lambda: h.statuses)
    assert h.statuses[0]["ok"] is True


def test_reset_clears_last_input_t(make):
    """`last_input_t` belongs to the current epoch: after an input
    restart the old input's last arrival time must not read as fresh
    (the feeder's pause freshness check reads it)."""
    h = make()
    h.link.reset(1)
    t = time.monotonic()
    h.link.submit(SMALL_A, t, 1)
    assert h.link.last_input_t == t
    h.link.reset(2)
    assert h.link.last_input_t is None
    h.link.submit(SMALL_A, t, 1)
    assert h.link.last_input_t is None


def test_old_epoch_submit_dropped_at_slot(make):
    h = make()
    h.link.reset(2)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(SMALL_A, time.monotonic(), 1)
    assert h.link.last_input_t is None
    t = time.monotonic()
    h.link.submit(SMALL_B, t, 2)
    assert h.link.last_input_t == t
    h.wait_for(lambda: h.outputs)
    assert [f for _, _, f in h.frames_received(0)] == [SMALL_B]
    assert [f for f, _, _ in h.outputs] == [SMALL_B]


def test_reset_drops_slot_frame_of_old_epoch(make, tmp_path):
    gate = tmp_path / "gate"
    h = make(envs=[{"FAKE_GATE": str(gate)}])
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(BIG, time.monotonic(), 1)
    h.wait_for(lambda: h.link._slot is None)  # the writer is blocked on BIG
    h.link.submit(SMALL_A, time.monotonic(), 1)
    h.link.reset(2)
    gate.touch()
    h.wait_received(0, lambda m: kinds(m) == [RESET, FRAME, RESET])
    h.link.submit(SMALL_B, time.monotonic(), 2)
    h.wait_for(lambda: any(f == SMALL_B for f, _, _ in h.outputs))
    assert [e for e, _, _ in h.frames_received(0)] == [1, 2]
    assert [f for _, _, f in h.frames_received(0)] == [BIG, SMALL_B]


def test_stale_slot_frame_not_sent(make, tmp_path):
    clock = FakeClock(100.0)
    gate = tmp_path / "gate"
    h = make(envs=[{"FAKE_GATE": str(gate)}], clock=clock)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(BIG, 100.0, 1)
    h.wait_for(lambda: h.link._slot is None)  # the writer is blocked on BIG
    h.link.submit(SMALL_A, 100.0, 1)
    clock.t = 100.3
    gate.touch()
    h.wait_received(0, lambda m: kinds(m) == [RESET, FRAME])
    h.wait_for(lambda: len(h.outputs) == 1)
    time.sleep(0.3)
    assert [f for _, _, f in h.frames_received(0)] == [BIG]
    assert h.link._slot is None


def test_old_epoch_output_dropped(make):
    h = make(envs=[{"FAKE_DELAY_MS": "300"}])
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(SMALL_A, time.monotonic(), 1)
    h.wait_received(0, lambda m: FRAME in kinds(m))
    h.link.reset(2)
    # The fake's STATUS after an echo trails the echoed FRAME on the pipe.
    h.wait_for(lambda: any(s.get("echoed") == 1 for s in h.statuses))
    assert h.outputs == []


def test_down_fires_before_backoff(make):
    h = make(envs=[{"FAKE_DIE_AFTER": "0"}], backoff_s=(30.0, 30.0), max_sleeps=1)
    h.link.reset(1)
    h.link.start()
    h.wait_for(lambda: h.sleeps)
    assert h.events[:3] == ["spawn", "down", ("sleep", 30.0)]
    assert h.link.up is False


def test_respawn_order(make):
    h = make(envs=[{"FAKE_DIE_AFTER": "1"}, {}])
    seen_at_spawn = []
    h.on_spawn_hook = lambda: (time.sleep(0.2),
                               seen_at_spawn.append(h.received(len(h.procs) - 1)))
    h.link.reset(7)
    h.link.start()
    h.wait_for(h.spawned(2))
    msgs = h.wait_received(1, lambda m: len(m) >= 1)
    assert seen_at_spawn == [[], []]
    assert msgs[0][0] == RESET
    assert decode_reset(msgs[0][1]) == 7
    assert h.events[:4] == ["spawn", "down", ("sleep", 1.0), "spawn"]


def test_respawn_sends_current_epoch(make):
    h = make(envs=[{"FAKE_DIE_AFTER": "1"}, {}])
    h.sleep_gate = threading.Event()
    h.link.reset(1)
    h.link.start()
    h.wait_for(lambda: h.sleeps)
    h.link.reset(2)  # while down, mid-backoff
    h.sleep_gate.set()
    h.wait_for(h.spawned(2))
    msgs = h.wait_received(1, lambda m: len(m) >= 1)
    assert decode_reset(msgs[0][1]) == 2


def test_respawn_reasserts_hold_empty_before_frames(make):
    h = make()
    h.link.reset(3)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.hold_grab()
    h.wait_received(0, lambda m: HOLD_GRAB in kinds(m))
    h.procs[0].kill()
    h.wait_for(h.spawned(2))
    h.link.submit(SMALL_A, time.monotonic(), 3)
    msgs = h.wait_received(1, lambda m: len(m) >= 3)
    assert kinds(msgs) == [RESET, HOLD_EMPTY, FRAME]
    assert decode_reset(msgs[0][1]) == 3


def test_release_then_respawn_sends_no_hold(make):
    h = make()
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.hold_empty()
    h.link.release()
    h.link.reset(2)  # release cleared the hold: reset re-sends none
    h.procs[0].kill()
    h.wait_for(h.spawned(2))
    h.link.submit(SMALL_A, time.monotonic(), 2)
    msgs = h.wait_received(1, lambda m: len(m) >= 2)
    assert kinds(msgs) == [RESET, FRAME]


def test_hold_before_start_reaches_first_child(make):
    h = make()
    h.link.hold_empty()
    h.link.start()
    h.wait_for(h.spawned(1))
    msgs = h.wait_received(0, lambda m: len(m) >= 2)
    assert kinds(msgs) == [RESET, HOLD_EMPTY]
    assert decode_reset(msgs[0][1]) == 0


def test_reset_reasserts_hold_before_frames(make):
    """The child's RESET drops its hold, so reset() re-sends the
    remembered one, as a respawn does: a feeder started paused (the flag
    file present) whose call starts an input must not have its child run
    live, unheld frames."""
    h = make()
    h.link.hold_empty()
    h.link.start()
    h.wait_for(h.spawned(1))
    h.wait_received(0, lambda m: len(m) >= 2)
    h.link.reset(1)
    h.link.submit(SMALL_A, time.monotonic(), 1)
    msgs = h.wait_received(0, lambda m: FRAME in kinds(m))
    assert kinds(msgs) == [RESET, HOLD_EMPTY, RESET, HOLD_EMPTY, FRAME]
    assert decode_reset(msgs[2][1]) == 1


def test_reset_turns_a_grab_into_empty(make):
    h = make()
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.hold_grab()
    h.link.reset(2)
    h.link.submit(SMALL_A, time.monotonic(), 2)
    msgs = h.wait_received(0, lambda m: FRAME in kinds(m))
    assert kinds(msgs) == [RESET, HOLD_GRAB, RESET, HOLD_EMPTY, FRAME]
    assert h.link._hold == "empty"


def test_respawn_clears_slot(make, tmp_path):
    clock = FakeClock(100.0)
    never = tmp_path / "never"
    h = make(envs=[{"FAKE_MODE": "hang", "FAKE_GATE": str(never)}, {}], clock=clock)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(BIG, 100.0, 1)
    h.wait_for(lambda: h.link._slot is None)  # the writer is blocked on BIG
    clock.t = 101.9
    h.link.submit(SMALL_A, 101.9, 1)  # still fresh at the respawn below
    clock.t = 102.05  # BIG sent 2.05 s ago, nothing back: watchdog
    h.wait_for(h.spawned(2))
    assert h.events[:3] == ["spawn", "down", ("sleep", 1.0)]
    h.wait_received(1, lambda m: RESET in kinds(m))
    time.sleep(0.2)  # a leftover slot frame would be on the wire by now
    h.link.submit(SMALL_B, 102.05, 1)
    h.wait_for(lambda: h.outputs)
    assert [f for _, _, f in h.frames_received(1)] == [SMALL_B]
    assert kinds(h.received(1))[0] == RESET


def test_respawn_drops_frame_submitted_while_down(make):
    clock = FakeClock(100.0)
    h = make(envs=[{"FAKE_DIE_AFTER": "1"}, {}], clock=clock)
    h.sleep_gate = threading.Event()
    h.link.reset(1)
    h.link.start()
    h.wait_for(lambda: h.sleeps)
    h.link.submit(SMALL_A, 100.0, 1)  # mid-backoff, fresh at the respawn
    h.sleep_gate.set()
    h.wait_for(h.spawned(2))
    h.wait_received(1, lambda m: RESET in kinds(m))
    time.sleep(0.2)  # a leftover slot frame would be on the wire by now
    h.link.submit(SMALL_B, 100.0, 1)
    h.wait_for(lambda: h.outputs)
    assert [f for _, _, f in h.frames_received(1)] == [SMALL_B]


def test_old_generation_messages_are_noops(make):
    h = make()
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.wait_for(lambda: h.statuses)
    old = h.link._gen
    h.procs[0].kill()
    h.wait_for(h.spawned(2))
    h.wait_for(lambda: len(h.statuses) == 2)  # both start-up STATUSes
    late = encode_frame_out(1, 5, False, SMALL_A)[5:]
    assert h.link._handle(old, FRAME, late) is False
    assert h.link._handle(old, STATUS, b'{"ok": false}') is False
    assert h.outputs == [] and len(h.statuses) == 2
    cur = h.link._gen
    h.link.stop()
    assert h.link._handle(cur, FRAME, late) is False
    assert h.outputs == []


def test_backoff_doubles_and_caps(make):
    h = make(envs=[{"FAKE_DIE_AFTER": "0"}], max_sleeps=7)
    h.link.start()
    h.wait_for(lambda: len(h.sleeps) == 7)
    assert h.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert h.events.count("down") == 7


def test_backoff_resets_after_healthy_output(make):
    clock = FakeClock(100.0)
    # spawns 0, 1: die at once; 2: echo, dies after RESET + 1 frame; 3: die
    h = make(envs=[{"FAKE_DIE_AFTER": "0"}, {"FAKE_DIE_AFTER": "0"},
                   {"FAKE_DIE_AFTER": "2"}, {"FAKE_DIE_AFTER": "0"}],
             clock=clock, max_sleeps=4)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(3))
    h.wait_for(lambda: h.statuses)  # start-up STATUS at 100.0
    clock.t = 160.5
    h.link.submit(SMALL_A, 160.5, 1)
    h.wait_for(lambda: len(h.sleeps) == 4)
    assert h.sleeps == [1.0, 2.0, 1.0, 2.0]


def test_watchdog_kills_hung_child_not_while_hold_empty(make):
    clock = FakeClock(100.0)
    h = make(envs=[{"FAKE_MODE": "hang"}, {}], clock=clock)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(SMALL_A, 100.0, 1)  # arms the watchdog...
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 1)
    h.link.hold_empty()  # ...and this disarms it
    clock.t = 100.25  # past fresh_s: the unanswered first frame no longer blocks the next (R7)
    h.link.submit(SMALL_A, 100.25, 1)  # sent under HOLD_EMPTY: never arms
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 2)
    clock.t = 105.0
    time.sleep(0.4)  # several watchdog polls
    assert "down" not in h.events

    h.link.release()
    h.link.submit(SMALL_B, 105.0, 1)
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 3)
    clock.t = 106.9
    time.sleep(0.3)
    assert "down" not in h.events
    clock.t = 107.05
    h.wait_for(lambda: "down" in h.events)
    h.wait_for(h.spawned(2))
    assert h.procs[0].wait(TIMEOUT_S) != 0


def test_watchdog_quiet_while_child_answers(make):
    clock = FakeClock(100.0)
    h = make(clock=clock)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    for i in range(5):
        t = 100.0 + i * 1.5
        clock.t = t
        h.link.submit(SMALL_A, t, 1)
        h.wait_for(lambda: len(h.outputs) == i + 1)
    time.sleep(0.2)
    assert "down" not in h.events


def test_submit_never_blocks(make, tmp_path):
    never = tmp_path / "never"
    h = make(envs=[{"FAKE_GATE": str(never)}])
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    t = time.monotonic()
    start = time.perf_counter()
    for _ in range(1000):
        h.link.submit(BIG, t, 1)
    assert time.perf_counter() - start < 0.05


def test_stop_kills_child_and_silences_callbacks(make):
    h = make()
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.wait_for(lambda: h.statuses)
    h.link.stop()
    assert h.procs[0].wait(TIMEOUT_S) is not None
    with h.cv:
        before = list(h.events), len(h.outputs), len(h.statuses)
    h.link.submit(SMALL_A, time.monotonic(), 1)
    time.sleep(0.3)
    with h.cv:
        assert (list(h.events), len(h.outputs), len(h.statuses)) == before
    assert "down" not in h.events
    assert h.link.up is False
    assert len(h.procs) == 1


def test_spawn_failure_counts_as_down(make):
    h = make(max_sleeps=2, argv=["/nonexistent/ffsb-camera-filter"])
    h.link.start()
    h.wait_for(lambda: len(h.sleeps) == 2)
    assert h.events[:4] == ["down", ("sleep", 1.0), "down", ("sleep", 2.0)]


def test_stop_from_on_spawn_leaves_link_down(make):
    h = make()
    h.on_spawn_hook = h.link.stop
    h.link.start()
    h.wait_for(lambda: "spawn" in h.events)
    assert h.procs[0].wait(TIMEOUT_S) is not None
    time.sleep(0.2)
    assert h.link.up is False
    assert h.events == ["spawn"]
    assert len(h.procs) == 1


def frames_sent_during(h, secs, hz=30):
    """Submits a BIG frame at `hz` for `secs` (real clock)."""
    end = time.monotonic() + secs
    n = 0
    while time.monotonic() < end:
        h.link.submit(BIG, time.monotonic(), 1)
        n += 1
        time.sleep(1 / hz)
    return n


FAKE_DELAY_S = 0.1  # the slow child's per-frame delay in test_one_frame_in_flight


def test_one_frame_in_flight(make):
    """R7: the next frame waits for the child's answer, so a slow child
    gets the newest frame each time it is free and its outputs are only
    as old as its own processing time (not twice it). The age bound is a
    gross-regression sanity check only, loose enough for a loaded check
    phase; test_second_frame_waits_while_first_in_flight is the one that
    tells one-in-flight from the old pipelining, without timing."""
    h = make(envs=[{"FAKE_DELAY_MS": str(round(FAKE_DELAY_S * 1000))}])
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    submitted = frames_sent_during(h, 1.0)
    time.sleep(0.3)
    received = len(h.frames_received(0))
    assert submitted >= 15
    assert 4 <= received <= 13, received
    ages = sorted(h.ages)
    assert ages, "no outputs"
    p90 = ages[math.ceil(len(ages) * 0.9) - 1]  # nearest rank: with ~10 samples, not the max
    assert p90 < FAKE_DELAY_S * 5, ages


def test_second_frame_waits_while_first_in_flight(make, tmp_path):
    """R7, deterministically: a child busy with F1 (it answers only when
    told) never gets F2 written to it; F2 waits in the slot until F1 is
    answered. The fake clock never moves, so the fresh_s timeout can't
    release it either."""
    clock = FakeClock(100.0)
    answer = tmp_path / "answer"
    h = make(envs=[{"FAKE_ANSWER_GATE": str(answer)}], clock=clock)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(SMALL_A, 100.0, 1)
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 1)
    h.link.submit(SMALL_B, 100.0, 1)
    time.sleep(0.3)  # time enough for a writer that doesn't wait to take it
    assert h.link._slot is not None and h.link._slot[0] == SMALL_B
    assert [f for _, _, f in h.frames_received(0)] == [SMALL_A]

    answer.touch()
    h.wait_for(lambda: len(h.outputs) == 1)
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 2)
    assert h.link._slot is None
    assert [f for _, _, f in h.frames_received(0)] == [SMALL_A, SMALL_B]


def test_in_flight_clears_after_fresh_s_when_child_silent(make):
    """A child that never answers (HOLD_EMPTY, a settings error) still
    gets a frame about every fresh_s, newest first."""
    h = make(envs=[{"FAKE_MODE": "hang"}])
    h.link.reset(1)
    h.link.hold_empty()  # keeps the watchdog out of it
    h.link.start()
    h.wait_for(h.spawned(1))
    frames_sent_during(h, 1.0)
    time.sleep(0.1)
    received = len(h.frames_received(0))
    assert 2 <= received <= 7, received


def test_in_flight_clears_on_reset(make):
    h = make(envs=[{"FAKE_MODE": "hang"}], fresh_s=5.0, watchdog_s=30.0)
    h.link.reset(1)
    h.link.start()
    h.wait_for(h.spawned(1))
    h.link.submit(SMALL_A, time.monotonic(), 1)
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 1)
    h.link.submit(SMALL_B, time.monotonic(), 1)  # held back: SMALL_A is unanswered
    time.sleep(0.2)
    assert len(h.frames_received(0)) == 1
    h.link.reset(2)
    h.link.submit(BIG, time.monotonic(), 2)
    h.wait_received(0, lambda m: kinds(m).count(FRAME) == 2)
    assert [(e, f) for e, _, f in h.frames_received(0)] == [(1, SMALL_A), (2, BIG)]
