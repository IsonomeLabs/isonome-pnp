#!/usr/bin/env python3
"""Demo: print active devices every second to prove PnP works."""

import time

from isonome_pnp.core import list_active


def main():
    print("Isonome PnP demo — unplug / replug hardware and watch the list change.")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            devices = list_active()
            if devices:
                print(f"[{time.strftime('%H:%M:%S')}] {len(devices)} active device(s):")
                for dev in devices:
                    did = dev.get("_device_id", "unknown")
                    stat = dev.get("status", "unknown")
                    print(f"  • {did:20s} [{stat}]")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] No active devices.")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
