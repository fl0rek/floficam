"""Which camera `--camera auto` uses: the hardware cameras PipeWire
offers, minus loopbacks and our own output. `list_devices()` talks to
PipeWire (a connect and a roundtrip), so the daemon only calls it on the
input's worker thread, never on the GLib main thread (pwinput.py)."""
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from floficam.pwinput import is_safe_target  # noqa: E402

PIPEWIRE_PROVIDER = "pipewiredeviceprovider"
_HARDWARE_APIS = ("v4l2", "libcamera")
_LOOPBACK_DRIVER = "v4l2 loopback"


@dataclass(frozen=True)
class Camera:
    target: str  # a PipeWire target-object: node.name, else object.serial
    description: str
    path: str | None


def list_devices() -> list[dict[str, str]]:
    """Every PipeWire `Video/Source` device's properties, as strings, plus
    `description` (its display name). Devices from other providers (the
    plain V4L2 one) are hidden: their names are not PipeWire targets."""
    if not Gst.is_initialized():
        Gst.init(None)
    pw = Gst.DeviceProviderFactory.get_by_name(PIPEWIRE_PROVIDER)
    if pw is None:
        return []
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", None)
    for name in monitor.get_providers():
        if name != PIPEWIRE_PROVIDER:
            pw.hide_provider(name)
    if not monitor.start():
        return []
    try:
        devices = monitor.get_devices()
    finally:
        monitor.stop()
    out = []
    for dev in devices:
        props = dev.get_properties()
        d = {}
        if props is not None:
            for i in range(props.n_fields()):
                key = props.nth_field_name(i)
                d[key] = str(props.get_value(key))
        d["description"] = dev.get_display_name()
        out.append(d)
    return out


def _is_hardware(d: Mapping[str, str]) -> bool:
    return (d.get("device.api") in _HARDWARE_APIS or "api.v4l2.path" in d
            or any(k.startswith("api.libcamera.") for k in d))


def _is_output(d: Mapping[str, str], output: str | None) -> bool:
    return any(d.get(k) and os.path.realpath(d[k]) == output for k in ("api.v4l2.path", "device.path"))


def usable(devices: list[Mapping[str, str]], output_device: str | None) -> list[Camera]:
    """The hardware cameras among `devices`, in order: no screencast or
    other virtual source, no v4l2loopback device, not our own output."""
    output = os.path.realpath(output_device) if output_device else None
    cams = []
    for d in devices:
        if not _is_hardware(d) or d.get("api.v4l2.cap.driver") == _LOOPBACK_DRIVER or _is_output(d, output):
            continue
        target = next((d[k] for k in ("node.name", "object.serial") if is_safe_target(d.get(k, ""))), None)
        if target is None:
            continue
        path = d.get("api.v4l2.path") or d.get("device.path") or None
        cams.append(Camera(target, d.get("description") or d.get("node.description") or target, path))
    return cams


def pick(output_device: str | None, *,
         list_devices: Callable[[], list[Mapping[str, str]]] = list_devices) -> Camera | None:
    """What `--camera auto` uses: the first usable camera, or None."""
    cams = usable(list_devices(), output_device)
    return cams[0] if cams else None
