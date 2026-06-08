"""I2C bus watcher.

Polls the I2C bus every 200 ms using smbus2.
Detects new devices (addresses responding to read_byte).
On detection: create registry file if missing, symlink to active.
On disappearance: remove symlink from active only.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Set

try:
    from smbus2 import SMBus
except ImportError:  # pragma: no cover
    SMBus = None

from isonome_pnp.core import (
    activate_device,
    deactivate_device,
    get_device,
    save_device,
)

logger = logging.getLogger("isonome_pnp.i2c")

# I2C reserved addresses (general call, CBUS, etc.)
RESERVED: Set[int] = set(range(0x00, 0x08)) | set(range(0x78, 0x80))


def _scan_bus(bus_number: int = 1) -> Set[int]:
    """Return a set of I2C addresses that respond on *bus_number*."""
    found: Set[int] = set()
    if SMBus is None:
        return found
    try:
        with SMBus(bus_number) as bus:
            for addr in range(0x03, 0x78):
                if addr in RESERVED:
                    continue
                try:
                    bus.read_byte(addr)
                    found.add(addr)
                except OSError:
                    pass
    except OSError as exc:
        logger.debug("SMBus %s unavailable: %s", bus_number, exc)
    return found


def _device_id_for(addr: int, taken: Set[str]) -> str:
    """Generate a unique device ID for an I2C address.

    If the plain hex address is already in *taken*, append an incrementing
    suffix (0x40_1, 0x40_2, …) as a placeholder for v2 serial support.
    """
    base = f"0x{addr:02X}"
    if base not in taken:
        return base
    n = 1
    while True:
        candidate = f"{base}_{n}"
        if candidate not in taken:
            return candidate
        n += 1


def _tick(bus_number: int, prev: Set[int]) -> Set[int]:
    """Single poll tick. Returns the new set of seen addresses."""
    now = _scan_bus(bus_number)

    added = now - prev
    removed = prev - now

    for addr in added:
        # Determine a unique device id, accounting for duplicates on the bus.
        active_dir = os.path.expanduser("~/.isonome/active")
        try:
            existing_active = {Path(f).stem for f in os.listdir(active_dir)}
        except FileNotFoundError:
            existing_active = set()
        device_id = _device_id_for(addr, existing_active)

        if get_device(device_id) is None:
            save_device(
                device_id,
                {"addr": f"0x{addr:02X}", "bus": bus_number, "status": "uncalibrated"},
            )
            logger.info("plugged 0x%02X (new registry entry %s)", addr, device_id)
        else:
            logger.info("plugged 0x%02X (existing %s)", addr, device_id)

        activate_device(device_id)
        # Print to stdout as required by spec
        print(f"plugged 0x{addr:02X}", flush=True)

    for addr in removed:
        # Find the device_id(s) that map to this address and deactivate them.
        # In the common case there is exactly one.
        active_dir = os.path.expanduser("~/.isonome/active")
        try:
            entries = os.listdir(active_dir)
        except FileNotFoundError:
            entries = []
        for entry in entries:
            path = os.path.join(active_dir, entry)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("addr", "") == f"0x{addr:02X}":
                    deactivate_device(Path(entry).stem)
                    print(f"unplugged 0x{addr:02X}", flush=True)
                    logger.info("unplugged 0x%02X (%s)", addr, entry)
            except Exception:
                pass

    return now


def run(bus_number: int = 1, interval: float = 0.2) -> None:
    """Blocking loop that watches the I2C bus."""
    logger.info("I2C watcher starting on bus %s (interval %.0f ms)", bus_number, interval * 1000)
    seen: Set[int] = set()
    while True:
        try:
            seen = _tick(bus_number, seen)
        except Exception as exc:
            logger.exception("Tick failed: %s", exc)
        time.sleep(interval)
