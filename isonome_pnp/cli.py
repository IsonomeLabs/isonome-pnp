"""CLI entry point: isonome."""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import click

from isonome_pnp import __version__

logger = logging.getLogger(__name__)
from isonome_pnp.core import (
    activate_device,
    ensure_dirs,
    get_device,
    list_active,
    save_calibration,
    save_device,
)
from isonome_pnp.udev_watcher import scan_csi, scan_i2c


def _user_systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _service_file() -> Path:
    return _user_systemd_dir() / "isonome-pnp.service"


@click.group()
@click.version_option(version=__version__, prog_name="isonome")
def cli():
    """Isonome PnP — plug-and-play hardware abstraction layer."""
    pass


@cli.group()
def pnp():
    """Plug-and-play commands."""
    pass


@pnp.command()
def init():
    """Create ~/.isonome, set up systemd user service, udev rules, i2c group."""
    ensure_dirs()
    click.echo("Created ~/.isonome/{registry,active}")

    # Systemd user service
    service_dir = _user_systemd_dir()
    service_dir.mkdir(parents=True, exist_ok=True)

    # Find the isonome executable in the current environment
    isonome_exe = shutil.which("isonome") or f"{sys.executable} -m isonome_pnp.cli"
    service_text = f"""[Unit]
Description=Isonome PnP hardware watcher

[Service]
Type=simple
ExecStart={isonome_exe} pnp start
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    _service_file().write_text(service_text)
    click.echo(f"Wrote systemd user service to {_service_file()}")

    # Try to reload systemd daemon (best effort)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        click.echo("Reloaded systemd user daemon")
    except FileNotFoundError:
        pass

    # udev rules for USB serial and video access without root
    rules_path = Path("/etc/udev/rules.d/99-isonome-pnp.rules")
    rules_text = """# Isonome PnP — allow user access to USB serial and video devices
SUBSYSTEM=="tty", ATTRS{idVendor}=="*", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="video4linux", MODE="0666", GROUP="plugdev"
"""
    if os.geteuid() == 0:
        rules_path.write_text(rules_text)
        click.echo(f"Wrote udev rules to {rules_path}")
        subprocess.run(["udevadm", "control", "--reload-rules"], check=False, capture_output=True)
        subprocess.run(["udevadm", "trigger"], check=False, capture_output=True)
    else:
        click.echo(
            f"[skipped] Run as root to install udev rules:\n"
            f"  sudo tee {rules_path} <<'EOF'{rules_text}EOF\n"
            f"  sudo udevadm control --reload-rules && sudo udevadm trigger"
        )

    # i2c group
    try:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        result = subprocess.run(
            ["groups", user],
            capture_output=True,
            text=True,
            check=False,
        )
        if "i2c" not in result.stdout:
            click.echo(f"[info] Add user to i2c group: sudo usermod -a -G i2c {user}")
        else:
            click.echo("User already in i2c group")
    except Exception:
        pass

    click.echo("\nSetup complete. Run 'isonome pnp start' to begin watching.")


@pnp.command()
@click.option("--debug", is_flag=True, help="Verbose logging to stdout")
def start(debug):
    """Launch the hardware watchers (foreground)."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    ensure_dirs()
    click.echo("Starting Isonome PnP watchers …")

    threads = []

    # I2C watcher
    def _i2c():
        from isonome_pnp.watcher import run as i2c_run

        i2c_run(bus_number=1, interval=0.2)

    t_i2c = threading.Thread(target=_i2c, daemon=True, name="i2c-watcher")
    t_i2c.start()
    threads.append(t_i2c)

    # USB/CSI watcher
    def _udev():
        from isonome_pnp.udev_watcher import run as udev_run

        try:
            udev_run()
        except Exception as exc:
            logger.warning("USB/CSI watcher unavailable: %s", exc)

    t_udev = threading.Thread(target=_udev, daemon=True, name="udev-watcher")
    t_udev.start()
    threads.append(t_udev)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nShutting down watchers …")
        sys.exit(0)


@pnp.command()
def status():
    """Print a tree of active devices and their calibration status."""
    devices = list_active()
    if not devices:
        click.echo("No active devices.")
        return

    click.echo("active/")
    for dev in devices:
        did = dev.get("_device_id", "unknown")
        stat = dev.get("status", "unknown")
        click.echo(f"  └── {did}.json  [{stat}]")


@pnp.command()
@click.argument("device_id")
@click.option("--json-data", help='Calibration JSON blob (e.g. \'{"zero": 512}\')')
def calibrate(device_id, json_data):
    """Calibrate a device by writing to its registry entry."""
    data = {}
    if json_data:
        try:
            data = json.loads(json_data)
        except json.JSONDecodeError as exc:
            click.echo(f"Invalid JSON: {exc}", err=True)
            sys.exit(1)
        if not isinstance(data, dict):
            click.echo("Calibration data must be a JSON object", err=True)
            sys.exit(1)
    else:
        # Interactive fallback
        click.echo(f"Enter calibration key=value pairs for {device_id}. Empty line to finish.")
        while True:
            line = click.prompt("", default="", show_default=False)
            line = line.strip()
            if not line:
                break
            if "=" not in line:
                click.echo("  Expected key=value", err=True)
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()

    save_calibration(device_id, data)
    click.echo(f"Calibration saved for {device_id}")


@pnp.command()
def scan():
    """Manual scan for CSI and I2C devices."""
    click.echo("Scanning I2C bus 1 …")
    i2c = scan_i2c(bus_number=1)
    for item in i2c:
        did = item["addr"]
        if get_device(did) is None:
            save_device(did, {**item, "status": "uncalibrated"})
            activate_device(did)
            click.echo(f"  found I2C {did} -> registered")
        else:
            activate_device(did)
            click.echo(f"  found I2C {did} -> already known, activated")

    click.echo("Scanning CSI cameras …")
    csi = scan_csi()
    for item in csi:
        serial = item.get("serial") or item["name"]
        did = f"camera_{serial}"
        if get_device(did) is None:
            save_device(did, {**item, "status": "uncalibrated"})
            activate_device(did)
            click.echo(f"  found CSI {item['devnode']} -> {did}")
        else:
            activate_device(did)
            click.echo(f"  found CSI {item['devnode']} -> {did} (already known)")


# Register `isonome pnp` as a subcommand group under the root cli
cli.add_command(pnp, name="pnp")


def main():
    cli()


if __name__ == "__main__":
    main()
