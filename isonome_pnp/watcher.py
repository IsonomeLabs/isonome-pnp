"""I2C bus watcher.

Polls the I2C bus using smbus2.
Detects new devices with a debounced quick-write/read probe.
On detection: create registry file if missing, symlink to active.
On disappearance: remove symlink from active only.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Set, Tuple

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

# Debounce thresholds: how many consecutive ticks a device must be seen or
# missed before we trust the state change.  This prevents registry write
# storms caused by noisy connections.
DEBOUNCE_PRESENT = 2
DEBOUNCE_ABSENT = 2

# Adaptive polling.  When the bus is stable we can scan slowly; when state is
# changing we scan quickly so plug/unplug feels responsive.
FAST_INTERVAL = 0.2
SLOW_INTERVAL = 1.0
STABLE_TICKS_BEFORE_SLOW = 10


def _probe_addr(bus, addr: int) -> bool:
    """Return True if a device acks at *addr*.

    A zero-byte quick write is the fastest, safest probe because it has no
    data phase.  Some adapters do not implement it, so we fall back to the
    traditional read_byte used by the original implementation.
    """
    try:
        bus.write_quick(addr)
        return True
    except OSError:
        pass
    try:
        bus.read_byte(addr)
        return True
    except OSError:
        return False


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
                if _probe_addr(bus, addr):
                    found.add(addr)
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


class StableScanner:
    """Debounced I2C scanner with adaptive polling intervals.

    The scanner keeps a per-address history so a single missed or spurious
    acknowledgement cannot cause a plug/unplug event.  Once the bus has been
    stable for a while it lowers the poll rate to reduce I2C bus traffic and
    CPU usage; any state change immediately returns to the fast rate.
    """

    def __init__(
        self,
        bus_number: int = 1,
        debounce_present: int = DEBOUNCE_PRESENT,
        debounce_absent: int = DEBOUNCE_ABSENT,
        fast_interval: float = FAST_INTERVAL,
        slow_interval: float = SLOW_INTERVAL,
        stable_ticks_before_slow: int = STABLE_TICKS_BEFORE_SLOW,
    ):
        self.bus_number = bus_number
        self.debounce_present = debounce_present
        self.debounce_absent = debounce_absent
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.stable_ticks_before_slow = stable_ticks_before_slow

        self.history: Dict[int, int] = {}
        self.confirmed: Set[int] = set()
        self.stable_ticks = 0

    def tick(self) -> Tuple[Set[int], Set[int], float]:
        """Perform one scan and return (added, removed, next_interval)."""
        seen = _scan_bus(self.bus_number)

        # Increment history for every address that acked this tick.
        for addr in seen:
            self.history[addr] = self.history.get(addr, 0) + 1

        # Decrement history only for addresses we currently believe are present.
        # This gives clean hysteresis: a new device needs DEBOUNCE_PRESENT
        # consecutive detections, and a disappearing device needs DEBOUNCE_ABSENT
        # consecutive misses, but it does not pay a penalty for never-before-seen
        # addresses suddenly appearing.
        for addr in self.confirmed:
            if addr not in seen:
                self.history[addr] = self.history.get(addr, 0) - 1

        new_confirmed = set(self.confirmed)
        for addr, count in list(self.history.items()):
            if addr not in new_confirmed and count >= self.debounce_present:
                new_confirmed.add(addr)
            elif addr in new_confirmed and count <= -self.debounce_absent:
                new_confirmed.discard(addr)
                # Reset history so a future re-plug is detected quickly.
                self.history[addr] = 0

        added = new_confirmed - self.confirmed
        removed = self.confirmed - new_confirmed
        self.confirmed = new_confirmed

        if added or removed:
            self.stable_ticks = 0
        else:
            self.stable_ticks += 1

        interval = (
            self.fast_interval
            if self.stable_ticks < self.stable_ticks_before_slow
            else self.slow_interval
        )
        return added, removed, interval


def _handle_added(addr: int, bus_number: int) -> None:
    """Create/activate a registry entry for a newly confirmed address."""
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
    print(f"plugged 0x{addr:02X}", flush=True)


def _handle_removed(addr: int) -> None:
    """Deactivate registry entries that reference a removed address."""
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


def _tick(scanner: StableScanner) -> float:
    """Single poll tick.  Returns the recommended interval until the next tick."""
    added, removed, interval = scanner.tick()

    for addr in added:
        try:
            _handle_added(addr, scanner.bus_number)
        except Exception as exc:
            logger.exception("Failed to handle added 0x%02X: %s", addr, exc)

    for addr in removed:
        try:
            _handle_removed(addr)
        except Exception as exc:
            logger.exception("Failed to handle removed 0x%02X: %s", addr, exc)

    return interval


def run(bus_number: int = 1, interval: float = FAST_INTERVAL) -> None:
    """Blocking loop that watches the I2C bus.

    *interval* is used as the fast polling interval; after the bus has been
    stable for a while the scanner automatically switches to SLOW_INTERVAL.
    """
    logger.info(
        "I2C watcher starting on bus %s (fast %.0f ms, slow %.0f ms)",
        bus_number,
        interval * 1000,
        SLOW_INTERVAL * 1000,
    )
    scanner = StableScanner(bus_number=bus_number, fast_interval=interval)
    while True:
        try:
            next_interval = _tick(scanner)
        except Exception as exc:
            logger.exception("Tick failed: %s", exc)
            next_interval = interval
        time.sleep(next_interval)
