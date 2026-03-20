# walkingpad-controller

Python library for controlling KingSmith WalkingPad treadmills over Bluetooth Low Energy (BLE).

Supports both **FTMS** (Fitness Machine Service) and legacy **WiLink** protocols behind a unified API. Protocol is auto-detected based on the BLE device name and advertised services.

## Features

- **Unified API** — single `WalkingPadController` class for all device types
- **Auto protocol detection** — FTMS for newer KS-HD-* devices, WiLink for older models
- **Real-time status** — speed, distance, duration, calories, steps via BLE notifications
- **Cold-start handling** — waits for belt to start moving and stabilize before sending speed commands, avoiding BLE disconnects on KingSmith devices
- **Reconnect recovery** — pending target speed is automatically re-applied after BLE reconnection
- **KingSmith extensions** — step counter via proprietary FTMS extension (bit 13)

## Installation

```bash
pip install walkingpad-controller
```

For legacy WiLink device support (older WalkingPad models):

```bash
pip install walkingpad-controller[wilink]
```

## Quick Start

```python
import asyncio
from bleak import BleakScanner
from walkingpad_controller import WalkingPadController

async def main():
    # Find your treadmill
    device = await BleakScanner.find_device_by_name("KS-HD-Z1D")

    # Create controller (protocol auto-detected from BLE name)
    controller = WalkingPadController(ble_device=device)

    # Connect
    await controller.connect()
    print(f"Protocol: {controller.protocol.value}")
    print(f"Speed range: {controller.min_speed}-{controller.max_speed} km/h")

    # Start the belt (runs at minimum speed)
    await controller.start()

    # Set desired speed
    await controller.set_speed(3.0)

    # Read status
    print(f"Speed: {controller.status.speed} km/h")
    print(f"Steps: {controller.status.steps}")

    # Stop and disconnect
    await controller.stop()
    await controller.disconnect()

asyncio.run(main())
```

## Status Callbacks

Register callbacks to receive real-time status updates:

```python
from walkingpad_controller import WalkingPadController, TreadmillStatus

def on_status(status: TreadmillStatus):
    print(f"Speed: {status.speed} km/h, Distance: {status.distance}m, "
          f"Duration: {status.duration}s, Calories: {status.calories}, "
          f"Steps: {status.steps}")

controller = WalkingPadController(ble_device=device)
controller.register_status_callback(on_status)
controller.register_disconnect_callback(lambda: print("Disconnected!"))
await controller.connect()
```

## API Reference

### WalkingPadController

The main entry point. Auto-detects protocol and delegates to the appropriate backend.

| Property / Method | Description |
|---|---|
| `protocol` | Detected protocol (`ProtocolType.FTMS` or `ProtocolType.WILINK`) |
| `connected` | Whether the device is currently connected |
| `status` | Current `TreadmillStatus` |
| `min_speed` / `max_speed` | Speed range in km/h (read from device for FTMS) |
| `speed_increment` | Speed step size in km/h |
| `connect()` | Connect and auto-detect protocol |
| `disconnect()` | Disconnect from the device |
| `start()` | Start the belt (runs at minimum speed) |
| `stop()` | Stop the belt |
| `set_speed(speed_kmh)` | Set speed (starts belt if stopped) |
| `switch_mode(mode)` | Switch operating mode (WiLink: auto/manual/standby) |
| `register_status_callback(cb)` | Register a `TreadmillStatus` callback |
| `register_disconnect_callback(cb)` | Register a disconnect callback |
| `update_ble_device(device)` | Update BLE device reference after rediscovery |
| `update_state()` | Poll / refresh current status from the device |

### TreadmillStatus

Dataclass with real-time treadmill data:

| Field | Type | Description |
|---|---|---|
| `belt_state` | `int` | 0=stopped, 1=active, 5=standby, 9=starting |
| `speed` | `float` | Current speed in km/h |
| `mode` | `int` | Operating mode (0=auto, 1=manual, 2=standby) |
| `distance` | `int` | Total distance in meters |
| `duration` | `int` | Elapsed time in seconds |
| `steps` | `int` | Step count (FTMS KingSmith extension) |
| `calories` | `int` | Total energy in kcal |
| `calories_per_hour` | `int` | Energy rate |
| `heart_rate` | `int` | Heart rate in bpm (if available) |
| `timestamp` | `float` | Unix timestamp of last update |

### Protocol-Specific Controllers

For advanced use, you can use the protocol controllers directly:

- **`FTMSController`** — FTMS protocol (newer KS-HD-* devices)
- **`WiLinkController`** — Legacy WiLink protocol (older WalkingPad models, requires `[wilink]` extra)

## Supported Devices

### FTMS Protocol (tested)
- KingSmith KS-Z1D (BLE name: `KS-HD-Z1D`)
- Other KingSmith devices with BLE names starting with `KS-HD-`

### WiLink Protocol (via ph4-walkingpad)
- WalkingPad A1, A1 Pro
- WalkingPad C1, C2
- Other models supported by [ph4-walkingpad](https://github.com/niclasku/ph4-walkingpad)

## Known Behavior

### FTMS Cold Start
KingSmith FTMS devices require a `START_OR_RESUME` command before the belt will accept speed commands. The library handles this automatically: it sends START, waits for the belt to report speed > 0 via treadmill data notifications, then waits an additional stabilization period before sending `SET_TARGET_SPEED`. This avoids the BLE disconnects that occur when speed commands are sent too early during motor startup.

### BLE Connection Drops
KingSmith FTMS devices may occasionally drop the BLE connection after a cold start due to firmware limitations. The library stores the pending target speed and automatically re-applies it after reconnection (with appropriate stabilization delay). When used with the Home Assistant integration's "Stay Connected" mode, this provides seamless recovery.

### Connection Exclusivity
Only one BLE client can connect to the treadmill at a time. If Home Assistant holds the connection, the KS Fit app cannot connect, and vice versa.

## Requirements

- Python 3.10+
- [bleak](https://github.com/hbldh/bleak) >= 0.20.0
- [ph4-walkingpad](https://github.com/niclasku/ph4-walkingpad) >= 1.0.0 (optional, for WiLink devices)

## License

MIT
