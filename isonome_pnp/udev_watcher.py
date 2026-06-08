"""USB/CSI watcher using pyudev.

Listens for kernel add/remove events on USB video devices and USB-serial
adapters.  CSI cameras have no hotplug, so a manual scan is provided.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pyudev
except ImportError:  # pragma: no cover
    pyudev = None

from isonome_pnp.core import (
    activate_device,
    deactivate_device,
    get_device,
    save_device,
)

logger = logging.getLogger("isonome_pnp.udev")

# Subsystems / devtypes we care about
USB_VIDEO_RE = re.compile(r"^video\d+$")
USB_SERIAL_RE = re.compile(r"^ttyUSB\d+$|^ttyACM\d+$")


def _serial_from_device(device) -> Optional[str]:
    """Extract a stable serial-like identifier from a udev device."""
    serial = device.get("ID_SERIAL_SHORT") or device.get("ID_SERIAL")
    if serial:
        return serial
    # Fallback: bus-port_path
    busnum = device.get("BUSNUM", "0")
    devpath = device.get("DEVPATH", "")
    return f"usb_{busnum}_{devpath.replace('/', '_')}"


def _device_id_for(prefix: str, serial: str, taken: set) -> str:
    """Generate a unique device ID for a USB/CSI device."""
    base = f"{prefix}_{serial}".replace("/", "_").replace("\\", "_")
    if base not in taken:
        return base
    n = 1
    while True:
        candidate = f"{base}_{n}"
        if candidate not in taken:
            return candidate
        n += 1


def _handle_add(device) -> None:
    """Process a udev 'add' event."""
    devnode: Optional[str] = device.device_node
    if not devnode:
        return

    name = os.path.basename(devnode)
    prefix: Optional[str] = None
    if USB_VIDEO_RE.match(name):
        prefix = "camera"
    elif USB_SERIAL_RE.match(name):
        prefix = "serial"
    else:
        return

    serial = _serial_from_device(device)
    if not serial:
        serial = name

    active_dir = Path.home() / ".isonome" / "active"
    taken = {f.stem for f in active_dir.iterdir()} if active_dir.exists() else set()
    device_id = _device_id_for(prefix, serial, taken)

    if get_device(device_id) is None:
        save_device(
            device_id,
            {
                "devnode": devnode,
                "serial": serial,
                "subsystem": device.subsystem,
                "status": "uncalibrated",
            },
        )
        logger.info("plugged %s (%s) -> %s", devnode, serial, device_id)
    else:
        logger.info("plugged %s (%s) existing %s", devnode, serial, device_id)

    activate_device(device_id)
    print(f"plugged {devnode}", flush=True)


def _handle_remove(device) -> None:
    """Process a udev 'remove' event."""
    devnode: Optional[str] = device.device_node
    if not devnode:
        return
    name = os.path.basename(devnode)
    if not (USB_VIDEO_RE.match(name) or USB_SERIAL_RE.match(name)):
        return

    # Remove any active symlink whose registry entry references this devnode.
    active_dir = Path.home() / ".isonome" / "active"
    registry_dir = Path.home() / ".isonome" / "registry"
    if not active_dir.exists():
        return
    for entry in active_dir.iterdir():
        reg = registry_dir / entry.name
        if not reg.is_file():
            continue
        try:
            data = json.loads(reg.read_text())
            if data.get("devnode") == devnode:
                deactivate_device(entry.stem)
                print(f"unplugged {devnode}", flush=True)
                logger.info("unplugged %s (%s)", devnode, entry.stem)
        except Exception:
            pass


def run() -> None:
    """Blocking loop that listens to udev events."""
    if pyudev is None:
        logger.error("pyudev is not installed; USB/CSI watcher cannot start")
        raise RuntimeError("pyudev is required for USB/CSI watching")

    try:
        context = pyudev.Context()
    except ImportError as exc:
        logger.error("pyudev context failed (libudev missing?): %s", exc)
        raise RuntimeError("pyudev requires libudev; is this Linux?") from exc

    monitor = pyudev.Monitor.from_netlink(context)
    # Filter by subsystem to avoid noise; we still inspect devnode inside handlers.
    monitor.filter_by("video4linux")
    monitor.filter_by("tty")
    monitor.filter_by("usb")

    logger.info("USB/CSI watcher starting")
    for device in iter(monitor.poll, None):
        if device.action == "add":
            _handle_add(device)
        elif device.action == "remove":
            _handle_remove(device)


def scan_csi() -> List[Dict[str, str]]:
    """Manual scan for CSI cameras (e.g. on Raspberry Pi).

    Looks for /dev/video* devices that are NOT associated with a USB bus.
    Returns a list of discovered device dicts.
    """
    found: List[Dict[str, str]] = []
    if pyudev is None:
        return found

    try:
        context = pyudev.Context()
    except ImportError:
        return found

    for device in context.list_devices(subsystem="video4linux"):
        devnode = device.device_node
        if not devnode:
            continue
        # CSI cameras typically have no ID_BUS or ID_BUS=="platform"
        id_bus = device.get("ID_BUS")
        if id_bus == "usb":
            continue
        name = os.path.basename(devnode)
        serial = _serial_from_device(device) or name
        found.append({"devnode": devnode, "serial": serial, "name": name})
    return found


def scan_i2c(bus_number: int = 1) -> List[Dict[str, str]]:
    """Manual scan for I2C devices (used by `isonome pnp scan`)."""
    found: List[Dict[str, str]] = []
    try:
        from smbus2 import SMBus
    except ImportError:
        return found

    try:
        with SMBus(bus_number) as bus:
            for addr in range(0x03, 0x78):
                if addr in {*range(0x00, 0x08), *range(0x78, 0x80)}:
                    continue
                try:
                    bus.read_byte(addr)
                    found.append({"addr": f"0x{addr:02X}", "bus": str(bus_number)})
                except OSError:
                    pass
    except OSError:
        pass
    return found
