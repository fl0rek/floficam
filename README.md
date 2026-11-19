# floficam

Virtual camera feeder for a sandboxed Firefox: a daemon reads a real V4L2 camera over PipeWire, runs frames through a filter child (effects, pause, preview) and publishes the result to a v4l2loopback device (`/dev/floficam` by default). `floficam-feed` is the daemon's PipeWire input stream, not its output.

Work in progress, not yet published.
