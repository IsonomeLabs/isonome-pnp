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


def _make_mock_bus(responding_addrs):
    """Return an SMBus mock where write_quick/read_byte only ack *responding_addrs*."""
    mock_bus = MagicMock()
    mock_bus.__enter__ = MagicMock(return_value=mock_bus)
    mock_bus.__exit__ = MagicMock(return_value=False)
    mock_bus.return_value = mock_bus  # SMBus(1) returns the mock instance

    def write_quick(addr):
        if addr not in responding_addrs:
            raise OSError("no ack")

    def read_byte(addr):
        if addr not in responding_addrs:
            raise OSError("no ack")
        return 0

    mock_bus.write_quick = write_quick
    mock_bus.read_byte = read_byte
    return mock_bus


class TestProbeAddr:
    def test_quick_write_success_skips_read_byte(self):
        bus = MagicMock()
        bus.write_quick = MagicMock()
        bus.read_byte = MagicMock()
        assert watcher._probe_addr(bus, 0x40) is True
        bus.write_quick.assert_called_once_with(0x40)
        bus.read_byte.assert_not_called()

    def test_falls_back_to_read_byte_when_quick_write_unsupported(self):
        bus = MagicMock()
        bus.write_quick = MagicMock(side_effect=OSError("not supported"))
        bus.read_byte = MagicMock(return_value=0)
        assert watcher._probe_addr(bus, 0x40) is True
        bus.write_quick.assert_called_once_with(0x40)
        bus.read_byte.assert_called_once_with(0x40)

    def test_returns_false_when_both_fail(self):
        bus = MagicMock()
        bus.write_quick = MagicMock(side_effect=OSError("no ack"))
        bus.read_byte = MagicMock(side_effect=OSError("no ack"))
        assert watcher._probe_addr(bus, 0x40) is False


class TestScanBus:
    def test_returns_empty_when_smbus_none(self):
        with patch.object(watcher, "SMBus", None):
            assert watcher._scan_bus(1) == set()

    def test_returns_addresses(self):
        mock_bus = _make_mock_bus({0x40, 0x41})
        with patch.object(watcher, "SMBus", mock_bus):
            result = watcher._scan_bus(1)
            assert result == {0x40, 0x41}

    def test_ignores_reserved_addresses(self):
        mock_bus = _make_mock_bus(set(range(0x80)))
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


class TestStableScanner:
    def test_requires_consecutive_ticks_to_add(self, monkeypatch):
        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=2, debounce_absent=2
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})

        added1, removed1, _ = scanner.tick()
        assert added1 == set()
        assert removed1 == set()

        added2, removed2, _ = scanner.tick()
        assert added2 == {0x40}
        assert removed2 == set()

    def test_requires_consecutive_ticks_to_remove(self, monkeypatch):
        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=1, debounce_absent=2
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        scanner.tick()
        assert scanner.confirmed == {0x40}

        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: set())
        # count goes 1 -> 0 -> -1 -> -2; removal happens at -2
        removed = set()
        for _ in range(5):
            _, removed_now, _ = scanner.tick()
            removed |= removed_now
        assert removed == {0x40}

    def test_single_flicker_does_not_change_state(self, monkeypatch):
        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=2, debounce_absent=2
        )
        responses = [{0x40}, set(), {0x40}, {0x40}]
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: responses.pop(0))

        for _ in range(4):
            scanner.tick()

        assert scanner.confirmed == {0x40}

    def test_adaptive_interval_slows_when_stable(self, monkeypatch):
        scanner = watcher.StableScanner(
            bus_number=1,
            debounce_present=1,
            debounce_absent=1,
            fast_interval=0.2,
            slow_interval=1.0,
            stable_ticks_before_slow=3,
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})

        _, _, interval1 = scanner.tick()
        _, _, interval2 = scanner.tick()
        _, _, interval3 = scanner.tick()
        _, _, interval4 = scanner.tick()

        assert interval1 == pytest.approx(0.2)
        assert interval2 == pytest.approx(0.2)
        assert interval3 == pytest.approx(0.2)
        assert interval4 == pytest.approx(1.0)

    def test_adaptive_interval_returns_to_fast_on_change(self, monkeypatch):
        scanner = watcher.StableScanner(
            bus_number=1,
            debounce_present=1,
            debounce_absent=1,
            fast_interval=0.2,
            slow_interval=1.0,
            stable_ticks_before_slow=3,
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        for _ in range(5):
            scanner.tick()
        assert scanner.tick()[2] == pytest.approx(1.0)

        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40, 0x41})
        _, _, interval = scanner.tick()
        assert interval == pytest.approx(0.2)


class TestTick:
    def test_adds_new_device_after_debounce(self, fake_home, monkeypatch):
        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=1, debounce_absent=1
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        watcher._tick(scanner)

        active = fake_home / ".isonome" / "active" / "0x40.json"
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        assert active.is_symlink()
        assert reg.exists()
        data = json.loads(reg.read_text())
        assert data["addr"] == "0x40"
        assert data["status"] == "uncalibrated"

    def test_removes_device_after_debounce(self, fake_home, monkeypatch):
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"addr": "0x40", "bus": 1}))
        lnk = fake_home / ".isonome" / "active" / "0x40.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))

        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=1, debounce_absent=1
        )
        scanner.confirmed = {0x40}
        # History already at the removal threshold so the handler fires on the
        # first empty scan.
        scanner.history[0x40] = -1
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: set())
        watcher._tick(scanner)
        assert not lnk.exists()

    def test_deactivates_active_file_even_without_registry(self, fake_home, monkeypatch):
        """If active has a regular file (not symlink) for an address,
        _tick should still deactivate it when the device disappears."""
        lnk = fake_home / ".isonome" / "active" / "0x40.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.write_text(json.dumps({"addr": "0x40", "bus": 1}))

        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=1, debounce_absent=1
        )
        scanner.confirmed = {0x40}
        # History already at the removal threshold so the handler fires on the
        # first empty scan.
        scanner.history[0x40] = -1
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: set())
        watcher._tick(scanner)
        # The active file should be removed even though there's no registry entry
        assert not lnk.exists()

    def test_reactivates_existing_registry_entry(self, fake_home, monkeypatch):
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"addr": "0x40", "bus": 1, "status": "calibrated"}))

        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=1, debounce_absent=1
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        watcher._tick(scanner)
        lnk = fake_home / ".isonome" / "active" / "0x40.json"
        assert lnk.is_symlink()
        data = json.loads(reg.read_text())
        assert data["status"] == "calibrated"

    def test_debounce_prevents_registry_write_storm(self, fake_home, monkeypatch):
        """A single spurious detection should not create a registry entry."""
        scanner = watcher.StableScanner(
            bus_number=1, debounce_present=2, debounce_absent=2
        )
        monkeypatch.setattr(watcher, "_scan_bus", lambda bus: {0x40})
        watcher._tick(scanner)

        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        assert not reg.exists()

        watcher._tick(scanner)
        assert reg.exists()
