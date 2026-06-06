"""Core filesystem API for Isonome PnP.

The filesystem is the source of truth:
~/.isonome/registry/  — permanent calibration database
~/.isonome/active/    — ephemeral presence state (symlinks to registry)
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


def _isonome_dir() -> Path:
    return Path.home() / ".isonome"


def _registry_dir() -> Path:
    return _isonome_dir() / "registry"


def _active_dir() -> Path:
    return _isonome_dir() / "active"


def _device_path(device_id: str) -> Path:
    """Return the registry path for a device_id (filename stem)."""
    return _registry_dir() / f"{device_id}.json"


def _active_link(device_id: str) -> Path:
    """Return the active symlink path for a device_id."""
    return _active_dir() / f"{device_id}.json"


def ensure_dirs() -> None:
    """Create ~/.isonome/{registry,active} if they don't exist."""
    _registry_dir().mkdir(parents=True, exist_ok=True)
    _active_dir().mkdir(parents=True, exist_ok=True)


def list_active() -> List[Dict[str, Any]]:
    """Return a list of dicts by reading ~/.isonome/active/ symlinks."""
    ensure_dirs()
    devices: List[Dict[str, Any]] = []
    active = _active_dir()
    if not active.exists():
        return devices
    for entry in active.iterdir():
        if entry.is_symlink() or entry.is_file():
            try:
                data = json.loads(entry.read_text())
                data["_device_id"] = entry.stem
                devices.append(data)
            except (json.JSONDecodeError, OSError):
                continue
    return devices


def get_device(device_id: str) -> Optional[Dict[str, Any]]:
    """Read a device from the registry."""
    path = _device_path(device_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_device(device_id: str, data: Dict[str, Any]) -> None:
    """Atomically write a device registry file."""
    ensure_dirs()
    path = _device_path(device_id)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_registry_dir(), prefix=".tmp_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def activate_device(device_id: str) -> None:
    """Symlink a registry entry into active/."""
    ensure_dirs()
    src = _device_path(device_id)
    dst = _active_link(device_id)
    if src.exists() and not dst.exists():
        dst.symlink_to(os.path.relpath(src, dst.parent))


def deactivate_device(device_id: str) -> None:
    """Remove a device's active symlink."""
    dst = _active_link(device_id)
    if dst.exists() or dst.is_symlink():
        dst.unlink()


def save_calibration(device_id: str, data: Dict[str, Any]) -> None:
    """Merge calibration data into an existing registry entry, or create one."""
    existing = get_device(device_id) or {}
    existing.update(data)
    existing["status"] = "calibrated"
    save_device(device_id, existing)
    activate_device(device_id)
