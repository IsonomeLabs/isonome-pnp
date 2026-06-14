"""Tests for isonome_pnp.core."""

import json
import os
from pathlib import Path

import pytest

from isonome_pnp import core


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestEnsureDirs:
    def test_creates_registry_and_active(self, fake_home):
        core.ensure_dirs()
        assert (fake_home / ".isonome" / "registry").is_dir()
        assert (fake_home / ".isonome" / "active").is_dir()


class TestListActive:
    def test_empty(self, fake_home):
        assert core.list_active() == []

    def test_reads_symlink(self, fake_home):
        core.ensure_dirs()
        reg = fake_home / ".isonome" / "registry" / "dev.json"
        reg.write_text(json.dumps({"status": "ok"}))
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))
        result = core.list_active()
        assert len(result) == 1
        assert result[0]["_device_id"] == "dev"
        assert result[0]["status"] == "ok"

    def test_skips_broken_symlink(self, fake_home):
        core.ensure_dirs()
        lnk = fake_home / ".isonome" / "active" / "broken.json"
        lnk.symlink_to("nonexistent.json")
        assert core.list_active() == []

    def test_skips_directories(self, fake_home):
        core.ensure_dirs()
        (fake_home / ".isonome" / "active" / "subdir").mkdir()
        assert core.list_active() == []

    def test_caches_until_directory_changes(self, fake_home):
        core.ensure_dirs()
        reg = fake_home / ".isonome" / "registry" / "dev.json"
        reg.write_text(json.dumps({"status": "ok"}))
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))

        first = core.list_active()
        # Mutate the registry file but not the active directory structure.
        reg.write_text(json.dumps({"status": "changed"}))
        cached = core.list_active()
        # Should return the cached parsed data.
        assert cached == first

        # Changing the active directory invalidates the cache.
        lnk2 = fake_home / ".isonome" / "active" / "other.json"
        lnk2.symlink_to(os.path.relpath(reg, lnk2.parent))
        invalidated = core.list_active()
        assert len(invalidated) == 2

    def test_save_device_invalidates_list_active_cache(self, fake_home):
        core.ensure_dirs()
        reg = fake_home / ".isonome" / "registry" / "dev.json"
        reg.write_text(json.dumps({"status": "ok"}))
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))

        core.list_active()
        core.save_device("dev", {"status": "updated"})
        result = core.list_active()
        assert result[0]["status"] == "updated"


class TestGetDevice:
    def test_missing(self, fake_home):
        assert core.get_device("missing") is None

    def test_found(self, fake_home):
        core.ensure_dirs()
        path = fake_home / ".isonome" / "registry" / "dev.json"
        path.write_text(json.dumps({"foo": "bar"}))
        assert core.get_device("dev") == {"foo": "bar"}


class TestSaveDevice:
    def test_creates_file(self, fake_home):
        core.save_device("dev", {"x": 1})
        path = fake_home / ".isonome" / "registry" / "dev.json"
        assert path.exists()
        assert json.loads(path.read_text()) == {"x": 1}

    def test_overwrites_existing(self, fake_home):
        core.save_device("dev", {"x": 1})
        core.save_device("dev", {"x": 2})
        path = fake_home / ".isonome" / "registry" / "dev.json"
        assert json.loads(path.read_text()) == {"x": 2}

    def test_noop_when_content_unchanged(self, fake_home):
        core.save_device("dev", {"a": 1, "b": 2})
        path = fake_home / ".isonome" / "registry" / "dev.json"
        mtime_before = path.stat().st_mtime_ns
        # Different key order, same data -> no disk write
        result = core.save_device("dev", {"b": 2, "a": 1})
        assert result is False
        assert path.stat().st_mtime_ns == mtime_before

    def test_rewrites_when_content_changes(self, fake_home):
        core.save_device("dev", {"a": 1})
        result = core.save_device("dev", {"a": 2})
        assert result is True
        path = fake_home / ".isonome" / "registry" / "dev.json"
        assert json.loads(path.read_text()) == {"a": 2}

    def test_rejects_path_traversal(self, fake_home):
        """device_id containing '..' should not escape the registry dir."""
        core.save_device("../escape", {"bad": True})
        # The file must NOT be created outside registry/
        assert not (fake_home / ".isonome" / "escape.json").exists()
        assert (fake_home / ".isonome" / "registry" / ".._escape.json").exists()


class TestActivateDevice:
    def test_creates_symlink(self, fake_home):
        core.save_device("dev", {"status": "ok"})
        core.activate_device("dev")
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        assert lnk.is_symlink()
        assert lnk.exists()

    def test_replaces_broken_symlink(self, fake_home):
        """If active contains a broken symlink, activate_device should replace it."""
        core.save_device("dev", {"status": "ok"})
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.symlink_to("missing.json")
        core.activate_device("dev")
        assert lnk.exists()
        assert lnk.readlink() != Path("missing.json")

    def test_replaces_wrong_target(self, fake_home):
        """If active symlink points to a different registry file, it should be corrected."""
        core.save_device("dev", {"status": "ok"})
        core.save_device("other", {"status": "other"})
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        other = fake_home / ".isonome" / "registry" / "other.json"
        lnk.symlink_to(os.path.relpath(other, lnk.parent))
        core.activate_device("dev")
        assert lnk.exists()
        # Should now point to dev's registry file
        resolved = os.path.realpath(lnk)
        expected = os.path.realpath(fake_home / ".isonome" / "registry" / "dev.json")
        assert resolved == expected

    def test_replaces_regular_file(self, fake_home):
        """If active contains a plain file instead of a symlink, replace it."""
        core.save_device("dev", {"status": "ok"})
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.write_text("not a symlink")
        core.activate_device("dev")
        assert lnk.is_symlink()
        assert lnk.exists()


class TestDeactivateDevice:
    def test_removes_symlink(self, fake_home):
        core.save_device("dev", {"status": "ok"})
        core.activate_device("dev")
        core.deactivate_device("dev")
        assert not (fake_home / ".isonome" / "active" / "dev.json").exists()

    def test_removes_broken_symlink(self, fake_home):
        core.ensure_dirs()
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.symlink_to("missing.json")
        core.deactivate_device("dev")
        assert not lnk.exists()

    def test_noop_when_missing(self, fake_home):
        core.deactivate_device("dev")
        assert not (fake_home / ".isonome" / "active" / "dev.json").exists()


class TestSaveCalibration:
    def test_merges_and_sets_status(self, fake_home):
        core.save_device("dev", {"base": 1})
        core.save_calibration("dev", {"zero": 512})
        dev = core.get_device("dev")
        assert dev["base"] == 1
        assert dev["zero"] == 512
        assert dev["status"] == "calibrated"

    def test_requires_dict(self, fake_home):
        """Calibration data must be a mapping so .update() works."""
        core.save_device("dev", {"base": 1})
        with pytest.raises((AttributeError, TypeError)):
            core.save_calibration("dev", None)
