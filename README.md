# floficam

Virtual camera feeder for a sandboxed Firefox: a daemon reads a real V4L2 camera over PipeWire, runs frames through a filter child (effects, pause, preview) and publishes the result as a PipeWire node named `floficam-feed`.

Work in progress, not yet published.
