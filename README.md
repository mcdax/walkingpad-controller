# walkingpad-controller

Python library for controlling KingSmith WalkingPad treadmills over Bluetooth Low Energy (BLE).

Supports **FTMS** (Fitness Machine Service), the legacy **WiLink** protocol and the **Sperax P3 Max** (wi-linktech WLT6200) behind a unified API. The protocol is auto-detected from the BLE device name and, if that is inconclusive, from the advertised services.

## Features

- **Unified API** — single `WalkingPadController` class for all device types
- **Auto protocol detection** — FTMS for newer KS-HD-* / KS-AP-* / KS-MC21-* / KS-SMC21C-* / ZP-ZEALR1-* devices, Sperax for `SPERAX_P3MAX`, WiLink for older models
- **Real-time status** — speed, distance, duration, calories, steps via BLE notifications
- **Cold-start handling** — waits for the belt to start moving before sending a speed command, avoiding BLE disconnects on KingSmith devices
- **Safe stop handling** — `stop()`/`pause()` cancel a pending start or speed change; start and speed changes wait while the belt runs down after STOP (see Known Behavior)
- **KingSmith vendor extensions** — step counter (FTMS bit 13), vendor pre-amble (any device exposing the `d18d2c10-…` characteristic, e.g. MC-21), supplement service detection
- **Firmware version** — exposed via `controller.firmware_version` (FTMS only)

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
| `protocol` | Detected protocol (`ProtocolType.FTMS`, `WILINK`, `SPERAX` or `UNKNOWN`) |
| `connected` | Whether the device is currently connected |
| `status` | Current `TreadmillStatus` |
| `min_speed` / `max_speed` | Speed range in km/h (read from device for FTMS) |
| `speed_increment` | Speed step size in km/h |
| `firmware_version` | Firmware version string (FTMS only; empty otherwise) |
| `connect()` | Connect and auto-detect protocol |
| `disconnect()` | Disconnect from the device |
| `start()` | Start or resume the belt (runs at minimum speed after a cold start) |
| `stop()` | End the session (counters reset) |
| `pause()` | Pause, keeping the counters; the next `start()` resumes (WiLink: falls back to stop) |
| `set_speed(speed_kmh)` | Set speed; starts the belt first if it isn't running. `0` pauses and never starts |
| `set_incline(step)` / `set_vibration(level)` | Sperax P3 Max only; `False` elsewhere |
| `switch_mode(mode)` | Switch operating mode (auto/manual/standby; support varies by device) |
| `register_status_callback(cb)` | Register a `TreadmillStatus` callback |
| `register_disconnect_callback(cb)` | Register a disconnect callback |
| `update_ble_device(device)` | Update BLE device reference for the next connect (e.g. after rediscovery) |
| `set_name(name)` | Name used for protocol detection when the `BLEDevice` has none (e.g. learned later) |
| `update_state()` | Poll / refresh current status from the device |

### TreadmillStatus

Dataclass with real-time treadmill data:

| Field | Type | Description |
|---|---|---|
| `belt_state` | `int` | `BeltState`: 0=stopped, 1=active, 2=paused, 5=standby, 9=starting |
| `speed` | `float` | Current speed in km/h |
| `mode` | `int` | Operating mode (0=auto, 1=manual, 2=standby) |
| `distance` | `int` | Total distance in meters |
| `duration` | `int` | Elapsed time in seconds |
| `steps` | `int` | Step count (FTMS KingSmith extension) |
| `calories` | `int` | Total energy in kcal |
| `calories_per_hour` | `int` | Energy rate |
| `heart_rate` | `int` | Heart rate in bpm (if available) |
| `training_status` / `last_fm_event` | `int` | FTMS training status and last Fitness Machine Status opcode |
| `incline` / `vibration_level` | `int` | Sperax P3 Max only |
| `timestamp` | `float` | Unix timestamp of last update |

### Protocol-Specific Controllers

For advanced use, you can use the protocol controllers directly:

- **`FTMSController`** — FTMS protocol (newer KS-HD-* devices)
- **`WiLinkController`** — Legacy WiLink protocol (older WalkingPad models, requires `[wilink]` extra)
- **`SperaxController`** — Sperax P3 Max (wi-linktech WLT6200, vendor service `0xFFF0`)

## Supported Devices

### FTMS Protocol (tested)
- KingSmith KS-Z1D (BLE name: `KS-HD-Z1D`) — confirmed working
- KingSmith KS-Z1E (BLE name: `KS-HD-Z1E`) — tested by community, see [#7](https://github.com/mcdax/walkingpad-controller/issues/7)
- KingSmith R3 (BLE name: `KS-AP-RQ3`, model `KS-R3Q`) — tested: connect, start, set_speed, pause, stop. Rejects STOP while the belt moves, so `stop()` pauses first (see Known Behavior)
- KingSmith MC-21 (BLE names: `KS-MC21-*`, `KS-SMC21C-*`, `ZP-ZEALR1-*`) — confirmed working as of v0.4.1, requires the vendor pre-amble described below
- Other KingSmith devices with BLE names starting with `KS-HD-` are expected to work via the same FTMS / supplement-service code path

Other FTMS treadmills (e.g. the Sperax RM-01) are detected by service probing and use the standard FTMS path.

### Sperax Protocol
- Sperax P3 Max (BLE name: `SPERAX_P3MAX…`, wi-linktech WLT6200) — speed, incline (0-10), vibration (0-4), pause

### WiLink Protocol (via ph4-walkingpad)
- WalkingPad A1, A1 Pro
- WalkingPad C1, C2
- Other models supported by [ph4-walkingpad](https://github.com/niclasku/ph4-walkingpad)

## Known Behavior

### FTMS Cold Start
KingSmith FTMS devices require a `START_OR_RESUME` command before the belt will accept speed commands. The library handles this automatically: it sends START, waits for the belt to report speed > 0 via treadmill data notifications, and only then sends `SET_TARGET_SPEED`. This avoids the BLE disconnects that occur when speed commands are sent during motor startup.

### Run-down After STOP
After a session-ending STOP the belt keeps moving for a few seconds. On the KS-HD-Z1D, a START or speed command sent during that run-down is acknowledged but has no effect. `start()` and `set_speed()` therefore wait until the belt reports 0 km/h before starting it again. The run-down is known from the library's own acknowledged STOP as well as from the device's status event.

### Halts Take Priority
`stop()` and `pause()` cancel a pending `start()` or `set_speed()`: an older start that is still waiting (for the belt to move, or for the run-down to end) gives up, and START or a speed command is not sent once a halt was requested. `set_speed()` decides between "set directly" and "start first" under the same lock as `stop()`.

### Reset and Control
FTMS `RESET` revokes control. The library requests control again before the next command (also when the RESET acknowledgement was lost); without it the KS-HD-Z1D answers nothing.

### STOP While Running
The KingSmith R3 (`KS-AP-*`) answers FTMS `STOP` with `OPERATION_FAILED` while the belt is moving, and the belt keeps running. KS Fit never sends STOP to a moving belt: it pauses, then ends the session. For devices listed in `FTMS_PAUSE_BEFORE_STOP_PREFIXES` (currently only `KS-AP-`, verified on the R3), `stop()` does the same:

1. It sends PAUSE whatever the cached speed is, because the cached value may be stale or lag a remote start.
2. It waits for Treadmill Data received *after* the PAUSE to report 0 km/h.
3. Only then does it send STOP.

If the link drops, telemetry stops arriving, or the belt is still moving at the timeout, STOP is not sent, `stop()` returns `False`, and the log says which case it was. Other FTMS devices keep the plain STOP behaviour.

The R3 also rejects both PAUSE and STOP during the 3-2-1 countdown after a START, and the belt then starts anyway. The firmware can't be halted during the countdown. If STOP is rejected at 0 km/h, `stop()` watches for the belt to start for a few seconds and pauses it as soon as it moves (on the R3 that takes about a second at 1 km/h). It then ends the session as above. If the belt stays at rest, there was no session to end and `stop()` returns `False`.

Multi-step commands (`start`, `stop`, `set_speed`, …) are serialised within a controller, and so are Control Point acknowledgements. `pause()` only waits for an in-flight Control Point write, and `stop()` cancels a pending start before queueing (see Halts Take Priority).

### BLE Connection Drops
KingSmith FTMS devices may occasionally drop the connection, typically around a cold start. The library does not reconnect or replay commands on its own: call `connect()` again (Home Assistant's integration does this) and re-send the speed if needed. After a reconnect the cached speed is reset to 0 until fresh telemetry arrives.

### Paused Session After Connecting
Connecting to a paused KS-HD-Z1D brings no status event. The library reads the first Treadmill Data frame instead: 0 km/h with elapsed time kept means a paused session (`BeltState.PAUSED`), 0 s means stopped.

### Remote Control and ESPHome Bluetooth Proxies
ESPHome Bluetooth proxies (as used by Home Assistant) keep the link at an 8.75-11.25 ms connection interval. At that interval the KS-HD-Z1D ignores its own remote control while connected. When connected through such a proxy, the library asks for 30-50 ms (the BlueZ default) right after connecting; the remote then keeps working. Older proxy firmware without this feature ignores the request.

### Connection Exclusivity
Only one BLE client can connect to the treadmill at a time — KS Fit and any client of this library are mutually exclusive.

### MC-21 Vendor Pre-amble
The MC-21 family (`KS-MC21-*`, `KS-SMC21C-*`, `ZP-ZEALR1-*`) requires a vendor pre-amble before each FTMS Control Point command, and acks via Fitness Machine Status events rather than Control Point indications. The library detects this and handles it transparently. See [docs/ftms-protocol-reference.md](docs/ftms-protocol-reference.md) for the protocol details and [docs/ks-fit-reverse-engineering.md](docs/ks-fit-reverse-engineering.md) for the analysis.

## Requirements

- Python 3.10+
- [bleak](https://github.com/hbldh/bleak) >= 0.20.0
- [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector) >= 3.0.0
- [ph4-walkingpad](https://github.com/niclasku/ph4-walkingpad) >= 1.0.0 (optional, for WiLink devices)

## License

MIT
