"""Tests for isonome_pnp.udev_watcher."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from isonome_pnp import udev_watcher


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestSerialFromDevice:
    def test_id_serial_short(self):
        dev = {"ID_SERIAL_SHORT": "ABC123"}
        assert udev_watcher._serial_from_device(dev) == "ABC123"

    def test_id_serial_fallback(self):
        dev = {"ID_SERIAL": "XYZ789"}
        assert udev_watcher._serial_from_device(dev) == "XYZ789"

    def test_usb_fallback(self):
        dev = {"BUSNUM": "1", "DEVPATH": "/devices/pci0000:00/1-2"}
        assert udev_watcher._serial_from_device(dev) == "usb_1__devices_pci0000:00_1-2"


class TestDeviceIdFor:
    def test_no_collision(self):
        assert udev_watcher._device_id_for("camera", "ABC", set()) == "camera_ABC"

    def test_collision(self):
        assert udev_watcher._device_id_for("camera", "ABC", {"camera_ABC"}) == "camera_ABC_1"

    def test_sanitizes_slashes_in_serial(self):
        """Serials containing '/' must not create subdirectories in the registry."""
        result = udev_watcher._device_id_for("camera", "foo/bar", set())
        assert "/" not in result
        assert result == "camera_foo_bar"


class TestHandleAdd:
    def test_creates_camera(self, fake_home):
        dev = MagicMock()
        dev.device_node = "/dev/video0"
        dev.get.side_effect = lambda key, default=None: {
            "ID_SERIAL_SHORT": "ABC123",
        }.get(key, default)
        dev.subsystem = "video4linux"
        type(dev).properties = property(lambda self: {})

        udev_watcher._handle_add(dev)
        reg = fake_home / ".isonome" / "registry" / "camera_ABC123.json"
        lnk = fake_home / ".isonome" / "active" / "camera_ABC123.json"
        assert reg.exists()
        assert lnk.is_symlink()

    def test_creates_serial(self, fake_home):
        dev = MagicMock()
        dev.device_node = "/dev/ttyUSB0"
        dev.get.side_effect = lambda key, default=None: {
            "ID_SERIAL_SHORT": "XYZ789",
        }.get(key, default)
        dev.subsystem = "tty"
        type(dev).properties = property(lambda self: {})

        udev_watcher._handle_add(dev)
        reg = fake_home / ".isonome" / "registry" / "serial_XYZ789.json"
        lnk = fake_home / ".isonome" / "active" / "serial_XYZ789.json"
        assert reg.exists()
        assert lnk.is_symlink()


class TestHandleRemove:
    def test_deactivates_by_devnode(self, fake_home):
        reg = fake_home / ".isonome" / "registry" / "cam.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"devnode": "/dev/video0"}))
        lnk = fake_home / ".isonome" / "active" / "cam.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))

        dev = MagicMock()
        dev.device_node = "/dev/video0"
        type(dev).properties = property(lambda self: {})

        udev_watcher._handle_remove(dev)
        assert not lnk.exists()

    def test_deactivates_broken_symlink(self, fake_home):
        """If active contains a broken symlink whose registry entry matches,
        it should still be removed."""
        reg = fake_home / ".isonome" / "registry" / "cam.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"devnode": "/dev/video0"}))
        lnk = fake_home / ".isonome" / "active" / "cam.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.symlink_to("missing.json")

        dev = MagicMock()
        dev.device_node = "/dev/video0"
        type(dev).properties = property(lambda self: {})

        udev_watcher._handle_remove(dev)
        assert not lnk.exists()


class TestScanCsi:
    def test_skips_usb_devices(self):
        mock_dev = MagicMock()
        mock_dev.device_node = "/dev/video0"
        mock_dev.get.return_value = "usb"
        mock_dev.subsystem = "video4linux"

        mock_context = MagicMock()
        mock_context.list_devices.return_value = [mock_dev]

        mock_pyudev = MagicMock()
        mock_pyudev.Context.return_value = mock_context

        with patch.object(udev_watcher, "pyudev", mock_pyudev):
            result = udev_watcher.scan_csi()
            assert result == []

    def test_includes_platform_devices(self):
        mock_dev = MagicMock()
        mock_dev.device_node = "/dev/video10"
        mock_dev.get.side_effect = lambda key, default=None: {
            "ID_SERIAL_SHORT": None,
            "ID_SERIAL": None,
            "BUSNUM": "1",
            "DEVPATH": "/devices/platform/foo",
        }.get(key, default)
        mock_dev.subsystem = "video4linux"

        mock_context = MagicMock()
        mock_context.list_devices.return_value = [mock_dev]

        mock_pyudev = MagicMock()
        mock_pyudev.Context.return_value = mock_context

        with patch.object(udev_watcher, "pyudev", mock_pyudev):
            result = udev_watcher.scan_csi()
            assert len(result) == 1
            assert result[0]["devnode"] == "/dev/video10"


class TestScanI2c:
    def test_returns_empty_when_smbus_missing(self):
        with patch.dict("sys.modules", {"smbus2": None}):
            assert udev_watcher.scan_i2c(1) == []
