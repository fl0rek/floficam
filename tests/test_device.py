from floficam.device import find_device


def _sysfs(tmp_path, entries):
    sysfs, dev = tmp_path / "s", tmp_path / "d"
    dev.mkdir()
    for n, name in entries:
        (sysfs / n).mkdir(parents=True)
        (sysfs / n / "name").write_text(name)
    return sysfs, dev


def test_explicit_wins(tmp_path):
    assert find_device("/dev/videoX", sysfs=tmp_path / "s", dev=tmp_path / "d") == "/dev/videoX"


def test_symlink_then_label(tmp_path):
    sysfs, dev = _sysfs(tmp_path, (("video0", "apple-isp"), ("video3", "floficam\n")))
    assert find_device(None, sysfs=sysfs, dev=dev) == str(dev / "video3")
    (dev / "floficam").touch()
    assert find_device(None, sysfs=sysfs, dev=dev) == str(dev / "floficam")


def test_symlink_ignored_for_other_label(tmp_path):
    sysfs, dev = _sysfs(tmp_path, (("video7", "vmtest"),))
    (dev / "floficam").touch()
    assert find_device(None, "vmtest", sysfs=sysfs, dev=dev) == str(dev / "video7")
    assert find_device(None, "nothere", sysfs=sysfs, dev=dev) is None


def test_none_found(tmp_path):
    assert find_device(None, sysfs=tmp_path / "missing", dev=tmp_path) is None
