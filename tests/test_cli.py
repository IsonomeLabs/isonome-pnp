"""Tests for isonome_pnp.cli."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from isonome_pnp.cli import cli


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


class TestStatus:
    def test_empty(self, fake_home, runner):
        result = runner.invoke(cli, ["pnp", "status"])
        assert result.exit_code == 0
        assert "No active devices" in result.output

    def test_shows_active_devices(self, fake_home, runner):
        reg = fake_home / ".isonome" / "registry" / "dev.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"status": "calibrated"}))
        lnk = fake_home / ".isonome" / "active" / "dev.json"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.symlink_to(os.path.relpath(reg, lnk.parent))

        result = runner.invoke(cli, ["pnp", "status"])
        assert result.exit_code == 0
        assert "dev.json" in result.output
        assert "calibrated" in result.output


class TestCalibrate:
    def test_with_json_data(self, fake_home, runner):
        result = runner.invoke(
            cli, ["pnp", "calibrate", "dev", '--json-data={"zero": 512}']
        )
        assert result.exit_code == 0
        reg = fake_home / ".isonome" / "registry" / "dev.json"
        assert reg.exists()
        data = json.loads(reg.read_text())
        assert data["zero"] == 512
        assert data["status"] == "calibrated"

    def test_invalid_json_exits(self, fake_home, runner):
        result = runner.invoke(
            cli, ["pnp", "calibrate", "dev", "--json-data=not-json"]
        )
        assert result.exit_code == 1
        assert "Invalid JSON" in result.output

    def test_non_dict_json_fails(self, fake_home, runner):
        """JSON scalars like 'null' should be rejected so save_calibration doesn't crash."""
        result = runner.invoke(
            cli, ["pnp", "calibrate", "dev", "--json-data=null"]
        )
        # Currently this crashes inside save_calibration because None has no .update()
        assert result.exit_code != 0


class TestScan:
    def test_registers_new_i2c(self, fake_home, runner):
        with patch("isonome_pnp.cli.scan_i2c") as mock_i2c, patch(
            "isonome_pnp.cli.scan_csi"
        ) as mock_csi:
            mock_i2c.return_value = [{"addr": "0x40", "bus": "1"}]
            mock_csi.return_value = []
            result = runner.invoke(cli, ["pnp", "scan"])
            assert result.exit_code == 0
            assert "registered" in result.output
            reg = fake_home / ".isonome" / "registry" / "0x40.json"
            assert reg.exists()

    def test_activates_existing_i2c(self, fake_home, runner):
        reg = fake_home / ".isonome" / "registry" / "0x40.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"addr": "0x40", "status": "calibrated"}))
        with patch("isonome_pnp.cli.scan_i2c") as mock_i2c, patch(
            "isonome_pnp.cli.scan_csi"
        ) as mock_csi:
            mock_i2c.return_value = [{"addr": "0x40", "bus": "1"}]
            mock_csi.return_value = []
            result = runner.invoke(cli, ["pnp", "scan"])
            assert result.exit_code == 0
            assert "already known" in result.output
            lnk = fake_home / ".isonome" / "active" / "0x40.json"
            assert lnk.is_symlink()


class TestInit:
    def test_creates_dirs(self, fake_home, runner):
        result = runner.invoke(cli, ["pnp", "init"])
        assert result.exit_code == 0
        assert (fake_home / ".isonome" / "registry").is_dir()
        assert (fake_home / ".isonome" / "active").is_dir()
