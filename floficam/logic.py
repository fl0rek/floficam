"""Pure (event, frame age) -> action logic for the camera feeder.

No V4L2, no GStreamer, no clock: callers always pass `now`. This is the
part the spec calls out as unit-tested; the V4L2 ioctls (v4l2.py) and the
GStreamer pipeline (the runtime, Task 4) sit behind thin adapters driven
by the `Act`s returned here. See
docs/superpowers/specs/2026-10-04-firefox-sandbox-camera-lane-design.md,
"The feeder" > Running and "Pure logic, tested", and
docs/superpowers/specs/2026-10-26-ffsb-camera-pause-design.md, "Pure logic"
for the pause/freeze additions, and
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "Pause,
re-plumbed", for pause as a hold inside the filter child: every frame
reaching the tick is the child's output, carrying its camera frame's
arrival time (`input_t`) and a `held` flag.
"""
import enum
from dataclasses import dataclass, replace

DEBOUNCE_S = 2.0
FRESH_S = 0.2
FLUSH_FRAMES = 2
# How long the last frame stays up through a gap before bars (--hold).
HOLD_DEFAULT_S = 10.0


class Act(enum.Enum):
    START_INPUT = enum.auto()
    STOP_INPUT = enum.auto()
    REASSERT_TIMEOUT = enum.auto()
    WRITE_REAL = enum.auto()
    WRITE_BARS = enum.auto()
    # The last output accepted before a pause press, written until the
    # child's held output arrives (no test-card blip at the press).
    WRITE_BRIDGE = enum.auto()
    FLUSH = enum.auto()
    EXIT_FAILURE = enum.auto()
    HOLD_GRAB = enum.auto()
    HOLD_EMPTY = enum.auto()
    RELEASE = enum.auto()


class PauseRefusal(enum.Enum):
    INPUT_DOWN = enum.auto()
    NO_FRESH_FRAME = enum.auto()
    ALREADY_FROZEN = enum.auto()
    FILTER_NOT_READY = enum.auto()


@dataclass
class State:
    usage: bool = False
    input_up: bool = False
    teardown_at: float | None = None
    # Input arrival time of the latest accepted filtered output.
    last_real: float | None = None
    paused: bool = False
    frozen: bool = False
    # Arrival time of the latest camera frame handed to the child.
    last_input: float | None = None
    last_real_held: bool = False
    # The child is up and its latest STATUS said ok.
    filter_ready: bool = False
    pause_t: float | None = None
    # last_real at the press, unless that output was itself held.
    bridge_t: float | None = None
    # A held output was accepted since the press.
    held_seen: bool = False
    # An output was accepted since the current reader started: only then
    # may the tick hold the last frame through a gap.
    hold_armed: bool = False


def _unpaused(s: State, acts: list, **changes) -> tuple[State, list]:
    """Every path that ends pause: both flags and the bridge cleared, and
    RELEASE appended when there was a pause to end."""
    new = replace(s, paused=False, frozen=False, pause_t=None, bridge_t=None,
                  held_seen=False, **changes)
    return new, acts + [Act.RELEASE] if s.paused else acts


def on_usage(s: State, level: int, now: float) -> tuple[State, list[Act]]:
    """A usage event: libwebrtc's capture STREAMON/STREAMOFF, as a level
    (the module's event queue holds one event, so edges in between are
    never seen)."""
    usage = bool(level)
    if usage:
        # A new reader (a 0->1 edge, inside the debounce too) must not be
        # shown the previous reader's last frame: disarm the hold until
        # the next accepted output.
        armed = s.hold_armed and s.usage
        # Cancel any pending teardown; a flap must not power-cycle the
        # input. Starting it is only needed once (idempotent).
        if s.input_up:
            return replace(s, usage=True, input_up=True, teardown_at=None,
                           hold_armed=armed), []
        return replace(s, usage=True, input_up=True, teardown_at=None,
                       hold_armed=armed), [
            Act.REASSERT_TIMEOUT,
            Act.START_INPUT,
        ]

    # usage == 0
    if not s.input_up:
        # Stale-flag rule: the input is already down, so a loaded
        # `paused=True` (e.g. from the flag file at start-up) describes a
        # call nobody is streaming into. Drop it now so the next call
        # cannot start on the test card.
        return _unpaused(s, [], usage=False)
    teardown_at = s.teardown_at
    if teardown_at is None:
        teardown_at = now + DEBOUNCE_S
    return replace(s, usage=False, teardown_at=teardown_at), []


def on_tick(s: State, now: float, hold_s: float = 0.0) -> tuple[State, list[Act]]:
    """One 30 Hz write tick. `hold_s`: how long the last output stays up
    through a gap before bars; it applies only while the filter is ready
    and an output arrived since the current reader started, otherwise
    the limit is FRESH_S."""
    if s.teardown_at is not None and now >= s.teardown_at:
        return _unpaused(s, [Act.FLUSH, Act.STOP_INPUT], input_up=False, teardown_at=None,
                         last_real=None)

    if not s.input_up:
        return s, []

    # Lock beats pause: no output within the limit (judged on its camera
    # frame's arrival time) writes bars, held or not.
    limit = max(FRESH_S, hold_s) if s.filter_ready and s.hold_armed else FRESH_S
    fresh = s.last_real is not None and now - s.last_real < limit
    if not s.paused:
        return s, [Act.WRITE_REAL] if fresh else [Act.WRITE_BARS]
    if not s.frozen:
        return s, [Act.WRITE_BARS]
    if s.held_seen:
        # While paused only held outputs move last_real (on_frame).
        return s, [Act.WRITE_REAL] if fresh else [Act.WRITE_BARS]
    if (s.bridge_t is not None and s.pause_t is not None and s.bridge_t <= s.pause_t
            and now - s.bridge_t < limit):
        return s, [Act.WRITE_BRIDGE]
    return s, [Act.WRITE_BARS]


def on_input(s: State, t: float | None) -> State:
    """The arrival time of the latest camera frame handed to the child
    (None: none since the current input started)."""
    return replace(s, last_input=t)


def accepts_frame(s: State, held: bool) -> bool:
    """Whether on_frame takes this output. While paused only held ones:
    an unheld output then comes from after the press (or a child that
    ignored the hold) and must never be written."""
    return held or not s.paused


def on_frame(s: State, input_t: float, held: bool) -> State:
    """A filtered output arrived from the child, for a camera frame that
    arrived at `input_t`."""
    if not accepts_frame(s, held):
        return s
    if s.paused:
        return replace(s, last_real=input_t, last_real_held=True, held_seen=True,
                       hold_armed=True)
    return replace(s, last_real=input_t, last_real_held=held, hold_armed=True)


def on_filter_status(s: State, ok: bool) -> State:
    """A STATUS from the child. `ok: false` means it is silent: whatever
    it held is not coming, so a frozen pause becomes paused-card."""
    if ok:
        return replace(s, filter_ready=True)
    # A recovered child says ok before its first output: until that output,
    # the frame from before the failure must not come back (no hold).
    return replace(s, filter_ready=False, frozen=False, hold_armed=False)


def on_filter_down(s: State) -> State:
    """The child exited (or was killed): its held frame is gone, and the
    hold waits for the next child's first output."""
    return replace(s, filter_ready=False, frozen=False, hold_armed=False)


def on_filter_spawn(s: State) -> tuple[State, list[Act]]:
    """A new child is starting. It holds nothing, so a pause is
    paused-card until florek freezes a new frame; never an auto-grab."""
    return replace(s, frozen=False), [Act.HOLD_EMPTY] if s.paused else []


def on_input_error(s: State) -> tuple[State, list[Act]]:
    """The GStreamer pipeline errored, or pipewiresrc disconnected."""
    if s.usage:
        return s, [Act.EXIT_FAILURE]
    # Usage 0 but still inside the debounce window: the input was up,
    # so the ring may hold real frames. Flush it to bars before
    # tearing down, same as on_tick's own teardown path — the ring
    # invariant (no real frame left over for the next reader) must
    # hold on every path that takes the input down, not just the
    # debounce-expiry one.
    acts = [Act.FLUSH, Act.STOP_INPUT] if s.input_up else [Act.STOP_INPUT]
    return _unpaused(s, acts, input_up=False, teardown_at=None, last_real=None)


def on_pause(s: State, now: float) -> tuple[State, list[Act], PauseRefusal | None]:
    """A pause request (hotkey or tray). Accepted only with the input up,
    the filter ready and a fresh camera frame (`last_input`: the child may
    be silent under HOLD_EMPTY), and only once (a second pause while
    already frozen is a no-op, refused). HOLD_GRAB has the child hold the
    next frame it gets; the tick bridges until then."""
    if not s.input_up:
        return s, [], PauseRefusal.INPUT_DOWN
    if s.frozen:
        return s, [], PauseRefusal.ALREADY_FROZEN
    if not s.filter_ready:
        return s, [], PauseRefusal.FILTER_NOT_READY
    if s.last_input is None or now - s.last_input >= FRESH_S:
        return s, [], PauseRefusal.NO_FRESH_FRAME
    bridge_t = s.last_real if not s.last_real_held else None
    return replace(s, paused=True, frozen=True, pause_t=now, bridge_t=bridge_t,
                   held_seen=False), [Act.HOLD_GRAB], None


def on_resume(s: State) -> tuple[State, list[Act]]:
    """A resume request. Always clears both flags and releases the
    child's hold (RELEASE even when not paused: harmless, and it repairs
    a hold the child might somehow still have)."""
    new, _ = _unpaused(s, [])
    return new, [Act.RELEASE]


def status(s: State) -> str:
    """The NOTIFY_SOCKET status string for the current state."""
    if not s.paused:
        return "live"
    if s.frozen:
        return "frozen"
    return "paused-card"
