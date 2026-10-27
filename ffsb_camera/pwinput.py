"""The PipeWire camera input: the GStreamer pipeline that exists exactly
while usage is 1 (spec: "The feeder" > Running). Not unit-tested against
real PipeWire — same as the rest of the GStreamer/V4L2 adapters, "Pure
logic, tested" keeps the pipeline itself behind a thin seam the live
checks cover; main.py's tests drive it through a fake, and
test_pwinput.py drives the threading below through a fake pipeline.

State changes never run on the GLib main thread (R9): with
`autoconnect=false`, `pipewiresrc`'s READY->PAUSED waits until the
capture script links the input and the format is negotiated, which with
the lane locked is never. Run there, it froze the 30 Hz tick and the
usage watch (scripts/test-ffsb-camera-feeder.sh, finding F1).
"""
import collections
import queue
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

WIDTH, HEIGHT = 1280, 720

# No `on-disconnect=error` (R12, reverting R7): locking the lane destroys
# the feed link, which pipewiresrc sees as a disconnect. With the default
# ("none") the input just starves and the tick writes bars; with "error"
# every lock during a call killed the feeder.
PIPELINE = (
    "pipewiresrc name=src autoconnect=false keepalive-time=0 resend-last=false "
    "client-name=ffsb-camera-feeder "
    'stream-properties="props,media.class=Stream/Input/Video,'
    'node.name=ffsb-camera-feed,firefox-sandbox.lane=camera-feed" ! '
    "videoconvert ! videoscale ! "
    f"video/x-raw,format=NV12,width={WIDTH},height={HEIGHT} ! "
    "appsink name=sink max-buffers=1 drop=true sync=false emit-signals=true"
)

CANCEL_RETRY_S = 0.05
# A state change still unfinished this long is reported (R13): pending
# for it, or running without being one of the waits below. While usage is
# 1 the report makes the feeder exit, and systemd restarts it, instead of
# it sitting forever behind a wedged worker.
JOB_DEADLINE_S = 10.0
WATCHDOG_TICK_S = 1.0


def _log(*parts):
    print("ffsb-camera-feeder:", *parts, file=sys.stderr, flush=True)


class _Job:
    """One queued state change. `waits`: once running it may legitimately
    block until cancel() (a start waiting for its link); its deadline then
    runs from the cancel instead."""

    def __init__(self, fn, on_overdue, waits):
        self.fn = fn
        self.on_overdue = on_overdue
        self.waits = waits
        self.since = time.monotonic()
        self.running = False
        self.reported = False

    def cancel(self) -> None:
        if self.waits:
            self.since = time.monotonic()  # before waits: the watchdog reads both
            self.waits = False


class _Worker:
    """One daemon thread running state changes in FIFO order, so a new
    input's start never overtakes the previous input's stop (two feeder
    inputs at once and the capture script links neither). Daemon, so a
    start still waiting for a link never holds up process exit.

    A raising job is logged and the worker carries on. A watchdog thread
    calls a job's `on_overdue` (once) when it has been pending, or running
    outside a legitimate wait, for longer than `deadline`."""

    def __init__(self, deadline=JOB_DEADLINE_S, tick=WATCHDOG_TICK_S):
        self.deadline = deadline
        self.tick = tick
        self._jobs = queue.Queue()
        self._live = []  # pending and running jobs, oldest first
        self._lock = threading.Lock()
        self._started = False

    def submit(self, fn, on_overdue=None, waits=False) -> _Job:
        job = _Job(fn, on_overdue, waits)
        with self._lock:
            self._live.append(job)
            if not self._started:
                self._started = True
                threading.Thread(target=self._run, name="ffsb-gst-state", daemon=True).start()
                threading.Thread(target=self._watch, name="ffsb-gst-watchdog", daemon=True).start()
        self._jobs.put(job)
        return job

    def _run(self):
        while True:
            job = self._jobs.get()
            job.running = True
            try:
                job.fn()
            except Exception as e:  # keep serving the queue
                _log("state change failed:", repr(e))
            finally:
                with self._lock:
                    self._live.remove(job)

    def _watch(self):
        while True:
            time.sleep(self.tick)
            now = time.monotonic()
            with self._lock:
                late = [j for j in self._live
                        if not j.reported and not (j.running and j.waits)
                        and now - j.since > self.deadline]
                for j in late:
                    j.reported = True
            for j in late:
                if j.on_overdue is not None:
                    j.on_overdue()


_WORKER = _Worker()


class LatestFrame:
    """Lock-guarded holder for the newest filtered frame, with its camera
    frame's arrival time and the child's `held` flag. put() runs on
    FilterLink's reader thread (its on_output callback); take() runs on
    the GLib main thread's 33 ms tick. Holds at most one frame: a tick
    that doesn't arrive in time to take one just sees it overwritten,
    which is the point (never repeat a stale real frame past the
    freshness window — logic.py enforces that window, this just avoids a
    torn read/write across threads)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None

    def put(self, frame: bytes, t: float, held: bool = False) -> None:
        with self._lock:
            self._frame = (frame, t, held)

    def take(self) -> tuple[bytes, float, bool] | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


# R14: a lock destroys the feed link, which clears pipewiresrc's format;
# a create() entered in that window returns GST_FLOW_NOT_NEGOTIATED and
# basesrc posts it as fatal (then EOS). That error rebuilds the pipeline
# instead of reaching Runner, at most this many times per window.
REBUILD_MAX = 3
REBUILD_WINDOW_S = 10.0


def is_lock_race(message, src) -> bool:
    """The not-negotiated flow error from our pipewiresrc: basesrc's
    GST_ELEMENT_FLOW_ERROR, a STREAM/FAILED error carrying
    details flow-return = GST_FLOW_NOT_NEGOTIATED."""
    if message.src is not src:
        return False
    err, _ = message.parse_error()
    if not err.matches(Gst.StreamError.quark(), Gst.StreamError.FAILED):
        return False
    details = message.parse_error_details()
    return details is not None and details.has_field("flow-return") \
        and details.get_value("flow-return") == int(Gst.FlowReturn.NOT_NEGOTIATED)


class _Attempt:
    """One pipeline of an input: the first from start(), more from R14
    rebuilds."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.start_done = threading.Event()
        self.start_job = None


class CameraInput:
    """Builds the pipeline above on start(), tears it down on stop().
    Both return at once; the state changes run on the shared worker
    thread. stop() cancels a start still waiting for its link by sending
    `pipewiresrc` a flush-start (its waits give up when flushing), retried
    until the start returns, since a flush that lands before the source's
    pad is active is reset by the activation.

    The appsink callback never touches State — it only calls
    `sink(frame, arrival time, epoch)` (FilterLink.submit) with the epoch
    it was built with; Runner gives every input a new epoch, so a frame
    racing stop() is dropped by the link. A bus ERROR or EOS calls
    `on_error` via `GLib.idle_add`, on the main thread; it is dropped once
    stop() has been called, or once its pipeline has been replaced. The
    lock-raced not-negotiated error (`is_lock_race`) instead rebuilds the
    pipeline, same sink and epoch, up to REBUILD_MAX times per
    REBUILD_WINDOW_S; past that it reports like any other error. A state
    change past the worker's deadline is reported through
    `on_error(text, overdue=True)` whether stopped or not (R13): a wedged
    worker would starve every later input too, so Runner treats
    it as fatal at any usage level."""

    def __init__(self, sink, epoch: int, on_error, make_pipeline=None, worker=None,
                 clock=time.monotonic):
        self.sink = sink
        self.epoch = epoch
        self.on_error = on_error
        self.make_pipeline = make_pipeline or (lambda: Gst.parse_launch(PIPELINE))
        self.worker = worker or _WORKER
        self.clock = clock
        self._attempt = None
        self._stopped = False
        self._rebuilds = collections.deque()

    def start(self) -> None:
        if not Gst.is_initialized():
            Gst.init(None)
        self._launch()

    def _launch(self) -> None:
        a = _Attempt(self.make_pipeline())
        a.pipeline.get_by_name("sink").connect("new-sample", self._on_new_sample)
        bus = a.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error_message, a)
        bus.connect("message::eos", self._on_eos_message, a)

        def play():
            try:
                if not self._stopped and self._attempt is a:
                    a.pipeline.set_state(Gst.State.PLAYING)
            finally:
                a.start_done.set()

        self._attempt = a
        a.start_job = self.worker.submit(
            play, on_overdue=lambda: self._report_overdue("start"), waits=True)

    def stop(self) -> None:
        if self._attempt is None:
            return
        self._stopped = True
        a, self._attempt = self._attempt, None
        self._retire(a)

    def _retire(self, a) -> None:
        a.pipeline.get_bus().remove_signal_watch()
        if a.start_job is not None:
            a.start_job.cancel()
        threading.Thread(target=self._cancel_start, args=(a,),
                         name="ffsb-gst-cancel", daemon=True).start()
        self.worker.submit(lambda: a.pipeline.set_state(Gst.State.NULL),
                           on_overdue=lambda: self._report_overdue("stop"))

    def _cancel_start(self, a) -> None:
        """Bounded: gives up after the worker's deadline, by which time
        the watchdog has reported the stuck start."""
        pad = a.pipeline.get_by_name("src").get_static_pad("src")
        give_up = time.monotonic() + self.worker.deadline
        while not a.start_done.is_set() and time.monotonic() < give_up:
            pad.send_event(Gst.Event.new_flush_start())
            a.start_done.wait(CANCEL_RETRY_S)

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        if self._stopped:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            self.sink(bytes(mapinfo.data), time.monotonic(), self.epoch)
        finally:
            buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_error_message(self, bus, message, a):
        err, debug = message.parse_error()
        text = f"{err}: {debug}" if debug else str(err)
        if is_lock_race(message, a.pipeline.get_by_name("src")):
            self._idle(lambda: self._rebuild(a, text))
        else:
            self._report_error(text, a)

    def _on_eos_message(self, bus, message, a):
        self._report_error("pipewiresrc reached EOS", a)

    def _rebuild(self, a, text: str) -> None:
        if self._stopped or self._attempt is not a:
            return
        now = self.clock()
        while self._rebuilds and now - self._rebuilds[0] >= REBUILD_WINDOW_S:
            self._rebuilds.popleft()
        if len(self._rebuilds) >= REBUILD_MAX:
            self.on_error(f"{text} (not-negotiated {len(self._rebuilds) + 1} times"
                          f" in {REBUILD_WINDOW_S:g} s)")
            return
        self._rebuilds.append(now)
        _log("input not-negotiated (feed link torn down mid-buffer): rebuilding")
        self._attempt = None
        self._retire(a)
        self._launch()

    def _report_error(self, text: str, a) -> None:
        def deliver():
            if not self._stopped and self._attempt is a:
                self.on_error(text)

        self._idle(deliver)

    def _report_overdue(self, what: str) -> None:
        text = f"pipeline {what} still unfinished after {self.worker.deadline:g} s"
        self._idle(lambda: self.on_error(text, overdue=True))

    @staticmethod
    def _idle(fn) -> None:
        def once():
            fn()
            return False

        GLib.idle_add(once)
