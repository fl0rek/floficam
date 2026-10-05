"""Pure (event, frame age) -> action logic for the camera feeder.

No V4L2, no GStreamer, no clock: callers always pass `now`. This is the
part the spec calls out as unit-tested; the V4L2 ioctls (v4l2.py) and the
GStreamer pipeline (the runtime, Task 4) sit behind thin adapters driven
by the `Act`s returned here. See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Running and "Pure logic, tested".
"""
import enum
from dataclasses import dataclass

DEBOUNCE_S = 2.0
FRESH_S = 0.2
FLUSH_FRAMES = 2


class Act(enum.Enum):
    START_INPUT = enum.auto()
    STOP_INPUT = enum.auto()
    REASSERT_TIMEOUT = enum.auto()
    WRITE_REAL = enum.auto()
    WRITE_BARS = enum.auto()
    FLUSH = enum.auto()
    EXIT_FAILURE = enum.auto()


@dataclass
class State:
    usage: bool = False
    input_up: bool = False
    teardown_at: float | None = None
    last_real: float | None = None


def on_usage(s: State, level: int, now: float) -> tuple[State, list[Act]]:
    """A usage event: libwebrtc's capture STREAMON/STREAMOFF, as a level
    (the module's event queue holds one event, so edges in between are
    never seen)."""
    usage = bool(level)
    if usage:
        # Cancel any pending teardown; a flap must not power-cycle the
        # input. Starting it is only needed once (idempotent).
        if s.input_up:
            return State(usage=True, input_up=True, teardown_at=None, last_real=s.last_real), []
        return State(usage=True, input_up=True, teardown_at=None, last_real=s.last_real), [
            Act.REASSERT_TIMEOUT,
            Act.START_INPUT,
        ]

    # usage == 0
    teardown_at = s.teardown_at
    if s.input_up and teardown_at is None:
        teardown_at = now + DEBOUNCE_S
    return State(usage=False, input_up=s.input_up, teardown_at=teardown_at, last_real=s.last_real), []


def on_tick(s: State, now: float) -> tuple[State, list[Act]]:
    """One 30 Hz write tick."""
    if s.teardown_at is not None and now >= s.teardown_at:
        return State(usage=s.usage, input_up=False, teardown_at=None, last_real=None), [
            Act.FLUSH,
            Act.STOP_INPUT,
        ]

    if not s.input_up:
        return s, []

    if s.last_real is not None and now - s.last_real < FRESH_S:
        return s, [Act.WRITE_REAL]
    return s, [Act.WRITE_BARS]


def on_frame(s: State, now: float) -> State:
    """A real frame arrived from the PipeWire input."""
    return State(usage=s.usage, input_up=s.input_up, teardown_at=s.teardown_at, last_real=now)


def on_input_error(s: State) -> tuple[State, list[Act]]:
    """The GStreamer pipeline errored, or pipewiresrc disconnected."""
    if s.usage:
        return s, [Act.EXIT_FAILURE]
    new_state = State(usage=s.usage, input_up=False, teardown_at=None, last_real=None)
    if s.input_up:
        # Usage 0 but still inside the debounce window: the input was up,
        # so the ring may hold real frames. Flush it to bars before
        # tearing down, same as on_tick's own teardown path — the ring
        # invariant (no real frame left over for the next reader) must
        # hold on every path that takes the input down, not just the
        # debounce-expiry one.
        return new_state, [Act.FLUSH, Act.STOP_INPUT]
    return new_state, [Act.STOP_INPUT]
