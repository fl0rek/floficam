"""Pure (event, frame age) -> action logic for the camera feeder.

No V4L2, no GStreamer, no clock: callers always pass `now`. This is the
part the spec calls out as unit-tested; the V4L2 ioctls (v4l2.py) and the
GStreamer pipeline (the runtime, Task 4) sit behind thin adapters driven
by the `Act`s returned here. See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Running and "Pure logic, tested", and
docs/superpowers/specs/2026-10-26-ffsb-camera-pause-design.md, "Pure logic"
for the pause/freeze additions.
"""
import enum
from dataclasses import dataclass, replace

DEBOUNCE_S = 2.0
FRESH_S = 0.2
FLUSH_FRAMES = 2


class Act(enum.Enum):
    START_INPUT = enum.auto()
    STOP_INPUT = enum.auto()
    REASSERT_TIMEOUT = enum.auto()
    WRITE_REAL = enum.auto()
    WRITE_BARS = enum.auto()
    WRITE_FROZEN = enum.auto()
    FLUSH = enum.auto()
    EXIT_FAILURE = enum.auto()
    FREEZE = enum.auto()
    THAW = enum.auto()


class PauseRefusal(enum.Enum):
    INPUT_DOWN = enum.auto()
    NO_FRESH_FRAME = enum.auto()
    ALREADY_FROZEN = enum.auto()


@dataclass
class State:
    usage: bool = False
    input_up: bool = False
    teardown_at: float | None = None
    last_real: float | None = None
    paused: bool = False
    frozen: bool = False


def on_usage(s: State, level: int, now: float) -> tuple[State, list[Act]]:
    """A usage event: libwebrtc's capture STREAMON/STREAMOFF, as a level
    (the module's event queue holds one event, so edges in between are
    never seen)."""
    usage = bool(level)
    if usage:
        # Cancel any pending teardown; a flap must not power-cycle the
        # input. Starting it is only needed once (idempotent).
        if s.input_up:
            return replace(s, usage=True, input_up=True, teardown_at=None), []
        return replace(s, usage=True, input_up=True, teardown_at=None), [
            Act.REASSERT_TIMEOUT,
            Act.START_INPUT,
        ]

    # usage == 0
    if not s.input_up:
        # Stale-flag rule: the input is already down, so a loaded
        # `paused=True` (e.g. from the flag file at start-up) describes a
        # call nobody is streaming into. Drop it now so the next call
        # cannot start on the test card.
        return replace(s, usage=False, paused=False, frozen=False), []
    teardown_at = s.teardown_at
    if teardown_at is None:
        teardown_at = now + DEBOUNCE_S
    return replace(s, usage=False, teardown_at=teardown_at), []


def on_tick(s: State, now: float) -> tuple[State, list[Act]]:
    """One 30 Hz write tick."""
    if s.teardown_at is not None and now >= s.teardown_at:
        return replace(
            s, input_up=False, teardown_at=None, last_real=None, paused=False, frozen=False
        ), [Act.FLUSH, Act.STOP_INPUT]

    if not s.input_up:
        return s, []

    if s.last_real is None or now - s.last_real >= FRESH_S:
        # Lock beats pause: no fresh frame always writes bars.
        return s, [Act.WRITE_BARS]
    if not s.paused:
        return s, [Act.WRITE_REAL]
    if s.frozen:
        return s, [Act.WRITE_FROZEN]
    return s, [Act.WRITE_BARS]


def on_frame(s: State, now: float) -> State:
    """A real frame arrived from the PipeWire input."""
    return replace(s, last_real=now)


def on_input_error(s: State) -> tuple[State, list[Act]]:
    """The GStreamer pipeline errored, or pipewiresrc disconnected."""
    if s.usage:
        return s, [Act.EXIT_FAILURE]
    new_state = replace(s, input_up=False, teardown_at=None, last_real=None, paused=False, frozen=False)
    if s.input_up:
        # Usage 0 but still inside the debounce window: the input was up,
        # so the ring may hold real frames. Flush it to bars before
        # tearing down, same as on_tick's own teardown path — the ring
        # invariant (no real frame left over for the next reader) must
        # hold on every path that takes the input down, not just the
        # debounce-expiry one.
        return new_state, [Act.FLUSH, Act.STOP_INPUT]
    return new_state, [Act.STOP_INPUT]


def on_pause(s: State, now: float) -> tuple[State, list[Act], PauseRefusal | None]:
    """A pause request (hotkey or tray). Accepted only with the input up
    and a fresh real frame, and only once (a second pause while already
    frozen is a no-op, refused)."""
    if not s.input_up:
        return s, [], PauseRefusal.INPUT_DOWN
    if s.frozen:
        return s, [], PauseRefusal.ALREADY_FROZEN
    if s.last_real is None or now - s.last_real >= FRESH_S:
        return s, [], PauseRefusal.NO_FRESH_FRAME
    return replace(s, paused=True, frozen=True), [Act.FREEZE], None


def on_resume(s: State) -> tuple[State, list[Act]]:
    """A resume request. Always clears both flags and tells the runtime
    to drop the frozen frame."""
    return replace(s, paused=False, frozen=False), [Act.THAW]


def status(s: State) -> str:
    """The NOTIFY_SOCKET status string for the current state."""
    if not s.paused:
        return "live"
    if s.frozen:
        return "frozen"
    return "paused-card"
