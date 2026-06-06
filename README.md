# isonome-pnp

Plug-and-play hardware abstraction layer for Isonome robotics.

> The filesystem is the source of truth. When hardware is plugged in, a JSON file appears. When it's unplugged, the file disappears. No APIs, no sockets, no complex daemon state.

## Quick Start

```bash
pip install isonome-pnp
isonome pnp init
isonome pnp start
```

That's it. Swapping servos and cameras now feels like plugging in a USB mouse.

## How it works

```
~/.isonome/
├── registry/          # permanent calibration database
│   ├── servo_0xA1B2.json
│   └── camera_0xFF12.json
└── active/            # ephemeral presence state (symlinks to registry)
    ├── servo_0xA1B2 -> ../registry/servo_0xA1B2.json
    └── camera_0xFF12 -> ../registry/camera_0xFF12.json
```

* **Registry** survives reboots and unplug events.
* **Active** reflects what is physically connected right now.

## CLI

| Command | Description |
|---------|-------------|
| `isonome pnp init` | Create `~/.isonome`, install systemd user service template, set udev rules, check i2c group |
| `isonome pnp start` | Launch I2C + USB watchers (foreground) |
| `isonome pnp status` | Print tree of active devices and calibration status |
| `isonome pnp calibrate <id>` | Interactively or via `--json-data` calibrate a device |
| `isonome pnp scan` | Manual scan for CSI cameras and I2C devices |

## Library API

```python
from isonome_pnp.core import list_active, get_device, save_calibration

for dev in list_active():
    print(dev["_device_id"], dev["status"])

save_calibration("servo_0x40", {"zero": 512, "range": 180})
```

## Demo

```bash
python demo.py
```

Prints active devices every second so you can unplug / replug hardware and watch the list change.

## systemd (user service)

After `isonome pnp init`, enable the watcher to start on login:

```bash
systemctl --user enable --now isonome-pnp.service
```

## License

MIT — Isonome Robotics
