"""Core filesystem API for Isonome PnP.

The filesystem is the source of truth:
~/.isonome/registry/  — permanent calibration database
~/.isonome/active/    — ephemeral presence state (symlinks to registry)
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _isonome_dir() -> Path:
    return Path.home() / ".isonome"


def _registry_dir() -> Path:
    return _isonome_dir() / "registry"


def _active_dir() -> Path:
    return _isonome_dir() / "active"


def _sanitize_device_id(device_id: str) -> str:
    """Strip path separators so a device_id cannot escape the registry dir."""
    return device_id.replace("/", "_").replace("\\", "_")


def _device_path(device_id: str) -> Path:
    """Return the registry path for a device_id (filename stem)."""
    return _registry_dir() / f"{_sanitize_device_id(device_id)}.json"


def _active_link(device_id: str) -> Path:
    """Return the active symlink path for a device_id."""
    return _active_dir() / f"{_sanitize_device_id(device_id)}.json"


def _canonical_json(data: Dict[str, Any]) -> str:
    """Return a stable, normalized JSON representation for comparison."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def ensure_dirs() -> None:
    """Create ~/.isonome/{registry,active} if they don't exist."""
    _registry_dir().mkdir(parents=True, exist_ok=True)
    _active_dir().mkdir(parents=True, exist_ok=True)


# Cache for list_active() keyed by directory identity (mtime + entry names).
_list_active_cache: Dict[str, Tuple[Any, List[Dict[str, Any]]]] = {}
_list_active_lock = threading.Lock()


def list_active() -> List[Dict[str, Any]]:
    """Return a list of dicts by reading ~/.isonome/active/ symlinks.

    Results are cached until the active directory's contents change.
    """
    ensure_dirs()
    active = _active_dir()

    try:
        stat = active.stat()
        entries = frozenset(p.name for p in active.iterdir())
    except (OSError, FileNotFoundError):
        return []

    cache_key = (stat.st_mtime_ns, stat.st_ino, entries)

    with _list_active_lock:
        cached_key, cached_devices = _list_active_cache.get("active", (None, None))
        if cached_key == cache_key and cached_devices is not None:
            return list(cached_devices)

    devices: List[Dict[str, Any]] = []
    for entry in active.iterdir():
        if entry.is_symlink() or entry.is_file():
            try:
                data = json.loads(entry.read_text())
                data["_device_id"] = entry.stem
                devices.append(data)
            except (json.JSONDecodeError, OSError):
                continue

    with _list_active_lock:
        _list_active_cache["active"] = (cache_key, list(devices))

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


def save_device(device_id: str, data: Dict[str, Any]) -> bool:
    """Atomically write a device registry file.

    Returns True if a write occurred, False if the file already contained
    identical data (no disk write was performed).
    """
    ensure_dirs()
    path = _device_path(device_id)
    payload = _canonical_json(data)

    # Avoid rewriting identical content — safe because JSON is normalized.
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
            if existing == payload:
                return False
        except OSError:
            pass

    tmp_fd, tmp_path = tempfile.mkstemp(dir=_registry_dir(), prefix=".tmp_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise

    # Invalidate the active cache so a stale list is never returned.
    with _list_active_lock:
        _list_active_cache.pop("active", None)

    return True


def activate_device(device_id: str) -> None:
    """Symlink a registry entry into active/."""
    ensure_dirs()
    src = _device_path(device_id)
    dst = _active_link(device_id)
    if src.exists():
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(os.path.relpath(src, dst.parent))
        with _list_active_lock:
            _list_active_cache.pop("active", None)


def deactivate_device(device_id: str) -> None:
    """Remove a device's active symlink."""
    dst = _active_link(device_id)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
        with _list_active_lock:
            _list_active_cache.pop("active", None)


def save_calibration(device_id: str, data: Dict[str, Any]) -> None:
    """Merge calibration data into an existing registry entry, or create one."""
    if not isinstance(data, dict):
        raise TypeError("Calibration data must be a dict")
    existing = get_device(device_id) or {}
    existing.update(data)
    existing["status"] = "calibrated"
    save_device(device_id, existing)
    activate_device(device_id)
