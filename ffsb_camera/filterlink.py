"""The feeder's side of the pipe to the filter child (`ffsb-camera-filter`):
spawning it, feeding it raw frames, reading its outputs, and respawning it
when it dies or hangs. See
docs/superpowers/specs/2026-10-26-ffsb-camera-effects-design.md, "The
feeder" > "Child management", and the plan's Global Constraints for the
timings pinned here.

Safety rests on three gates, all under one lock:
- `submit` drops a frame whose epoch isn't the one of the last `reset()`,
  and `reset()` empties the slot, so a late frame from a stopped input
  never reaches the child;
- the writer drops a slot frame older than `fresh_s` at send time, so a
  slow or blocked child never gets a frame from before a lock;
- the reader drops an output whose epoch isn't current, or which comes
  from a child generation that has since been replaced.

Every child generation gets its own writer, reader and supervisor threads,
all daemons. A generation counter turns an old generation's threads into
no-ops. Callbacks run on those threads, never the caller's, under one
re-entrant delivery lock that `reset()` and `stop()` also take: once
`reset(e)` returns, no output of another epoch is delivered, and once
`stop()` returns, no callback runs at all.
"""
import collections
import json
import subprocess
import sys
import threading
import time

from .filter.protocol import (
    FRAME,
    HOLD_EMPTY,
    HOLD_GRAB,
    RELEASE,
    STATUS,
    Parser,
    decode_frame_out,
    encode_control,
    encode_frame_in,
    encode_reset,
)

# How often the supervisor checks for an exit and the hang watchdog.
POLL_S = 0.05
_READ_CHUNK = 1 << 20


def _log(*parts):
    print("ffsb-camera-feeder:", *parts, file=sys.stderr, flush=True)


class _Generation:
    """One child process and what is known about its liveness."""

    def __init__(self, n, proc):
        self.n = n
        self.proc = proc
        self.eof = threading.Event()  # the reader saw EOF on stdout
        self.first_out = None  # clock() of the first message back
        self.last_out = None


class FilterLink:
    """Owns the filter child. `submit` may be called from any thread and
    never blocks; the other methods are meant for one controlling thread
    (the feeder's main loop).

    Hold intent survives a respawn (controller ruling R2): after
    `hold_grab()` or `hold_empty()` every new child is sent `HOLD_EMPTY`
    right after its `RESET` (a grab whose frame died with the old child
    becomes empty); `release()` clears it, `reset()` keeps it."""

    def __init__(self, argv, on_output, on_status, on_spawn, on_down,
                 clock=time.monotonic, sleep=time.sleep, popen=subprocess.Popen,
                 backoff_s=(1.0, 30.0), healthy_reset_s=60.0, watchdog_s=2.0,
                 fresh_s=0.2):
        self._argv = list(argv)
        self._on_output = on_output
        self._on_status = on_status
        self._on_spawn = on_spawn
        self._on_down = on_down
        self._clock = clock
        self._sleep = sleep
        self._popen = popen
        self._backoff_min, self._backoff_max = backoff_s
        self._healthy_reset_s = healthy_reset_s
        self._watchdog_s = watchdog_s
        self._fresh_s = fresh_s

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        # Taken around every callback and by reset()/stop(); never while
        # holding _lock. Re-entrant so a callback may call back in.
        self._deliver = threading.RLock()
        self._started = False
        self._stopped = False
        self._n = 0  # current generation number
        self._gen = None  # the live _Generation, None while down
        self._epoch = 0
        self._slot = None  # (frame, input_t, epoch), newest submit wins
        self._controls = collections.deque()  # encoded, sent ahead of frames
        self._hold = "none"  # "none" | "grab" | "empty": the last hold call
        self._pending_since = None  # clock() of the first frame sent since the last message back
        self._last_input_t = None
        self._delay = self._backoff_min

    # --- caller side -------------------------------------------------

    @property
    def last_input_t(self):
        with self._lock:
            return self._last_input_t

    @property
    def up(self):
        with self._lock:
            return self._gen is not None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            n = self._n
        self._launch(n)

    def stop(self) -> None:
        with self._deliver:
            with self._lock:
                if self._stopped:
                    return
                self._stopped = True
                self._n += 1
                gen, self._gen = self._gen, None
                self._slot = None
                self._controls.clear()
                self._cv.notify_all()
        if gen is not None:
            gen.proc.kill()

    def submit(self, frame: bytes, input_t: float, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                return
            self._slot = (frame, input_t, epoch)
            self._last_input_t = input_t
            self._cv.notify_all()

    def reset(self, epoch: int) -> None:
        with self._deliver:
            with self._lock:
                self._epoch = epoch
                self._slot = None
                # The current epoch's: an old input's last arrival time
                # must not read as fresh to the feeder's pause check.
                self._last_input_t = None
                if self._gen is not None:
                    self._controls.append(encode_reset(epoch))
                    self._cv.notify_all()

    def hold_grab(self) -> None:
        self._set_hold("grab", HOLD_GRAB)

    def hold_empty(self) -> None:
        self._set_hold("empty", HOLD_EMPTY)

    def release(self) -> None:
        self._set_hold("none", RELEASE)

    def _set_hold(self, hold: str, kind: int) -> None:
        with self._lock:
            self._hold = hold
            # A hold change restarts the watchdog's window: frames sent
            # under HOLD_EMPTY were never expected to answer.
            self._pending_since = None
            if self._gen is not None:
                self._controls.append(encode_control(kind))
                self._cv.notify_all()

    # --- generations -------------------------------------------------

    def _launch(self, n: int) -> None:
        threading.Thread(target=self._supervise, args=(n,),
                         name=f"ffsb-filter-supervisor-{n}", daemon=True).start()

    def _spawn(self, n: int):
        """Starts generation `n`: the child, then in order the slot
        cleared, `on_spawn()`, `RESET(current epoch)`, `HOLD_EMPTY` if a
        hold is remembered, and only then the writer (so only then any
        FRAME). Holding the delivery lock keeps `reset()` and `stop()`
        out of the middle of it (but not out of the fork itself, so the
        caller's thread never waits on one). None if the child could not
        start or `n` is no longer current."""
        try:
            proc = self._popen(self._argv, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, bufsize=0)
        except OSError as e:
            _log("filter child failed to start:", repr(e))
            return None
        gen = _Generation(n, proc)
        with self._deliver:
            with self._lock:
                current = n == self._n
                self._slot = None
                self._controls.clear()
                self._pending_since = None
            if current:
                self._call(self._on_spawn)
            with self._lock:
                # stop() ran during the fork, or from on_spawn itself.
                if n != self._n:
                    proc.kill()
                    proc.wait()
                    return None
                self._controls.append(encode_reset(self._epoch))
                if self._hold != "none":
                    self._hold = "empty"
                    self._controls.append(encode_control(HOLD_EMPTY))
                # Only now `up`: a hold call made before this point only
                # sets the intent re-asserted above, never queues ahead
                # of the RESET.
                self._gen = gen
        threading.Thread(target=self._write, args=(gen,),
                         name=f"ffsb-filter-writer-{n}", daemon=True).start()
        threading.Thread(target=self._read, args=(gen,),
                         name=f"ffsb-filter-reader-{n}", daemon=True).start()
        return gen

    def _supervise(self, n: int) -> None:
        gen = self._spawn(n)
        if gen is not None:
            self._watch(gen)
        self._exited(n)

    def _watch(self, gen: _Generation) -> None:
        """Returns once the child is gone: EOF on its stdout, reaped, or
        killed here by the hang watchdog or by stop()."""
        while not gen.eof.wait(POLL_S) and gen.proc.poll() is None:
            with self._lock:
                if gen.n != self._n:
                    break
                # Never armed under HOLD_EMPTY: see _write and _set_hold.
                hung = (self._pending_since is not None
                        and self._clock() - self._pending_since >= self._watchdog_s)
            if hung:
                _log(f"filter child silent {self._watchdog_s:g} s after a frame: killing it")
                break
        gen.proc.kill()
        gen.proc.wait()

    def _exited(self, n: int) -> None:
        """Generation `n` is over (or never started): `on_down()`, the
        backoff sleep, then the next generation. A no-op once stop() has
        run or a newer generation is current."""
        with self._deliver:
            with self._lock:
                if self._stopped or n != self._n:
                    return
                gen, self._gen = self._gen, None
                self._n += 1
                nxt = self._n
                self._controls.clear()
                self._cv.notify_all()
                if (gen is not None and gen.first_out is not None
                        and gen.last_out - gen.first_out >= self._healthy_reset_s):
                    self._delay = self._backoff_min
                delay = self._delay
                self._delay = min(delay * 2, self._backoff_max)
            _log(f"filter child down; respawning in {delay:g} s")
            self._call(self._on_down)
        self._sleep(delay)
        with self._lock:
            if self._stopped or nxt != self._n:
                return
        self._launch(nxt)

    # --- per-generation threads ---------------------------------------

    def _write(self, gen: _Generation) -> None:
        stdin = gen.proc.stdin
        try:
            while True:
                with self._lock:
                    while gen.n == self._n and not self._controls and self._slot is None:
                        self._cv.wait()
                    if gen.n != self._n:
                        return
                    if self._controls:
                        data = self._controls.popleft()
                    else:
                        frame, input_t, epoch = self._slot
                        self._slot = None
                        if self._clock() - input_t >= self._fresh_s:
                            continue
                        data = encode_frame_in(epoch, round(input_t * 1e9), frame)
                        # Before the write: a child that stops reading
                        # blocks it, and must still trip the watchdog.
                        if self._pending_since is None and self._hold != "empty":
                            self._pending_since = self._clock()
                _write_all(stdin, data)
        except (OSError, ValueError):
            pass  # the child is gone; the supervisor handles it
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _read(self, gen: _Generation) -> None:
        stdout = gen.proc.stdout
        parser = Parser()
        try:
            while True:
                data = stdout.read(_READ_CHUNK)
                if not data:
                    return
                for kind, payload in parser.feed(data):
                    if not self._handle(gen, kind, payload):
                        return
        except (OSError, ValueError):
            pass
        finally:
            gen.eof.set()
            try:
                stdout.close()
            except OSError:
                pass

    def _handle(self, gen: _Generation, kind: int, payload: bytes) -> bool:
        """One message from the child. False once `gen` is stale."""
        if kind == FRAME:
            epoch, pts_ns, held, frame = decode_frame_out(payload)
            args = (frame, pts_ns / 1e9, held)
            callback = self._on_output
        elif kind == STATUS:
            try:
                args = (json.loads(payload),)
            except ValueError as e:
                _log("filter child sent bad STATUS:", repr(e))
                args = None
            epoch = None
            callback = self._on_status
        else:
            _log(f"filter child sent unknown message type {kind}")
            return True
        with self._deliver:
            with self._lock:
                if gen.n != self._n:
                    return False
                now = self._clock()
                if gen.first_out is None:
                    gen.first_out = now
                gen.last_out = now
                self._pending_since = None
                current = epoch is None or epoch == self._epoch
            if current and args is not None:
                self._call(callback, *args)
        return True

    def _call(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:  # a broken callback must not kill the pipe
            _log("filter callback failed:", repr(e))


def _write_all(f, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = f.write(view)
        view = view[n:]
