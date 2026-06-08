"""Tests for isonome_pnp.watcher."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from isonome_pnp import watcher


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestScanBus:
    def test_returns_empty_when_smbus_none(self):
        with patch.object(watcher, "SMBus", None):
            assert watcher._scan_bus(1) == set()

    def test_returns_addresses(self):
        mock_bus = MagicMock()
        mock_bus.__enter__ = MagicMock(return_value=mock_bus)
        mock_bus.__exit__ = MagicMock(return_value=False)
        mock_bus.return_value = mock_bus  # SMBus(1) returns mock_bus

        def read_byte(addr):
            if addr in (0x40, 0x41):
                return 0
            raise OSError("no ack")

        mock_bus.read_byte = read_byte
        with patch.object(watcher, "SMBus", mock_bus):
            result = watcher._scan_bus(1)
            assert result == {0x40, 0x41}

    def test_ignores_reserved_addresses(self):
        mock_bus = MagicMock()
        mock_bus.__enter__ = MagicMock(return_value=mock_bus)
        mock_bus.__exit__ = MagicMock(return_value=False)
        with patch.object(watcher, "SMBus", mock_bus):
            result = watcher._scan_bus(1)
            for addr in result:
                assert addr not in watcher.RESERVED


class TestDeviceIdFor:
    def test_no_collision(self):
        assert watcher._device_id_for(0x40, set()) == "0x40"

    def test_collision(self):
        assert watcher._device_id_for(0x40, {"0x40"}) == "0x40_1"
        assert watcher._device_id_for(0x40, {"0x40", "0x40_1"}) == "0x40_2"


class TestTick:
    def test_adds_new_device(self, fake_home, monkeypatch):
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        watcher._tick(1, set())
        active = fake_home / ".isonome" / "active" / "0x40.json"
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        assert active.is_symlink()
        assert reg.exists()
        data = json.loads(reg.read_text())
        assert data["addr"] == "0x40"
        assert data["status"] == "uncalibrated"

    def test_removes_device(self, fake_home, monkeypatch):
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"addr": "0x40", "bus": 1}))
        lnk = fake_home / ".isonome" / "active" / "0x40.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))

        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: set())
        watcher._tick(1, {0x40})
        assert not lnk.exists()

    def test_deactivates_active_file_even_without_registry(self, fake_home, monkeypatch):
        """If active has a regular file (not symlink) for an address,
        _tick should still deactivate it when the device disappears."""
        lnk = fake_home / ".isonome" / "active" / "0x40.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.write_text(json.dumps({"addr": "0x40", "bus": 1}))

        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: set())
        watcher._tick(1, {0x40})
        # The active file should be removed even though there's no registry entry
        assert not lnk.exists()

    def test_reactivates_existing_registry_entry(self, fake_home, monkeypatch):
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"addr": "0x40", "bus": 1, "status": "calibrated"}))
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        watcher._tick(1, set())
        lnk = fake_home / ".isonome" / "active" / "0x40.json"
        assert lnk.is_symlink()
        data = json.loads(reg.read_text())
        assert data["status"] == "calibrated"
