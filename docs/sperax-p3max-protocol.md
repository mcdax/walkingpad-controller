# Sperax P3 Max — BLE Protocol

Reverse-engineered protocol for the **Sperax P3 Max** walking pad / vibration treadmill
(advertised BLE name `SPERAX_P3MAX`). Tracking: [issue #3](https://github.com/mcdax/hass-walkingpad/issues/3).

The device is built around a **wi-linktech WLT6200** BLE module
(GATT *Manufacturer Name* = `wi-linktech`, *Model Number* = `WLT6200`).
It does **not** use FTMS (`0x1826`) and it is **not** the legacy KingSmith WiLink
protocol (`0xFE00` / `0xFE01` / `0xFE02`). It uses a custom framed protocol on a
vendor service, documented below.

All command and status frames in this document were validated against a full,
unfiltered HCI snoop capture: **146/146 distinct frames pass CRC**, and a from-scratch
encoder reproduces every captured frame byte-for-byte.

---

## 1. GATT layout

| Role | Service | Characteristic | Handle | Properties |
|------|---------|----------------|--------|------------|
| **Command (write)**  | `0000fff0-0000-1000-8000-00805f9b34fb` | `0000fff2-…` | `0x002c` | Write **No Response** |
| **Status (notify)**  | `0000fff0-0000-1000-8000-00805f9b34fb` | `0000fff1-…` | `0x0029` | Notify |

To talk to the device:

1. Connect.
2. Enable notifications on `0xFFF1` (write `0x0001` to its CCCD, handle `0x002a`).
3. Send the **hello** command (§4.1) once.
4. Send a **status-poll** command (§4.4) periodically (the app does ~3×/s) to keep
   the status stream flowing, and send control commands as needed.

> A second vendor service `0x0000ff10-…` (`0xFF11` notify / `0xFF12` write) is present
> on the device but is **not** used by the official app for control. Ignore it.

The device also exposes a standard **Device Information** service (`0x180A`):
`Manufacturer = wi-linktech`, `Model = WLT6200`, `SW Rev = R22_V227.04.03`,
`HW Rev = V1.0.0`.

---

## 2. Frame format

Every frame — in both directions — has the same envelope:

```
F5 | LEN | 00 | CMD | [ ARGS… ] | CRC_lo | CRC_hi | FA
```

| Field | Size | Description |
|-------|------|-------------|
| `F5`      | 1 | Start-of-frame delimiter |
| `LEN`     | 1 | **Transmitted** (post-stuffing) total frame length, in bytes |
| `00`      | 1 | Constant channel/address byte (always `0x00`) |
| `CMD`     | 1 | Command / message type (see §4, §5) |
| `ARGS`    | n | Command arguments (may be empty) |
| `CRC`     | 2 | CRC-16, little-endian (see §3) |
| `FA`      | 1 | End-of-frame delimiter |

### 2.1 Byte stuffing

`0xF5`, `0xFA` and `0xF0` are reserved. Any **logical** byte in the range
`0xF0`–`0xFF` that would otherwise appear in the body (i.e. in `00 | CMD | ARGS | CRC`,
**not** the `F5`/`LEN`/`FA` framing) is escaped:

```
encode:  0xFn   ->   F0 0n          (emit 0xF0, then the low nibble)
decode:  F0 0n   ->   0xF0 | 0n     (= 0xFn)
```

Examples: `0xFA → F0 0A`, `0xF5 → F0 05`, `0xF4 → F0 04`, `0xFF → F0 0F`.

Because stuffing changes the byte count, `LEN` is the **stuffed** length as it appears
on the wire, while the CRC is computed over the **de-stuffed** bytes (see §3).

---

## 3. Checksum (CRC-16)

| Parameter | Value |
|-----------|-------|
| Width     | 16 bits |
| Polynomial | `0xA327` (reflected form, as used directly below) |
| Init      | `0xFFFF` |
| Reflected | yes (input and output) |
| XOR out   | `0x0000` |
| Byte order in frame | **little-endian** (`CRC_lo` first) |

The CRC is computed over the **de-stuffed** byte sequence
`[ 0xF5, LOGICAL_LEN, 0x00, CMD, ARGS… ]`, where `LOGICAL_LEN` is the de-stuffed frame
length (which equals `LEN` when no stuffing occurred).

Reference implementation:

```python
def crc16(data: bytes) -> int:
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA327 if (c & 1) else (c >> 1)
    return c & 0xFFFF
```

### 3.1 Full encoder / decoder

```python
def stuff(b: bytes) -> bytes:
    out = bytearray()
    for x in b:
        if 0xF0 <= x <= 0xFF:
            out += bytes([0xF0, x & 0x0F])
        else:
            out.append(x)
    return bytes(out)

def destuff(b: bytes) -> bytes:
    out = bytearray(); i = 0
    while i < len(b):
        if b[i] == 0xF0 and i + 1 < len(b):
            out.append(0xF0 | b[i + 1]); i += 2
        else:
            out.append(b[i]); i += 1
    return bytes(out)

def encode(inner: bytes) -> bytes:
    # inner = the logical body WITHOUT framing/CRC, i.e. [0x00, CMD, ARGS...]
    logical_len = 1 + 1 + len(inner) + 2 + 1          # F5 + LEN + inner + CRC(2) + FA
    c = crc16(bytes([0xF5, logical_len]) + inner)
    body = stuff(inner + bytes([c & 0xFF, (c >> 8) & 0xFF]))
    tx_len = 1 + 1 + len(body) + 1                     # actual on-wire length
    return bytes([0xF5, tx_len]) + body + bytes([0xFA])

def decode(frame: bytes) -> bytes:
    # returns the logical inner body [0x00, CMD, ARGS...] (CRC verified)
    full = bytes([0xF5]) + destuff(frame[1:-1]) + bytes([0xFA])
    data = bytearray(full[:-3]); data[1] = len(full)   # LOGICAL_LEN for CRC
    lo, hi = full[-3], full[-2]
    assert crc16(bytes(data)) == (hi << 8 | lo), "CRC mismatch"
    return full[2:-3]
```

---

## 4. Commands (app → device, characteristic `0xFFF2`)

All commands share the `F5 | LEN | 00 | CMD | … | CRC | FA` envelope. The
**inner body** column below is `00 | CMD | ARGS` (i.e. what `encode()` takes / `decode()`
returns).

| CMD | Meaning | Inner body | Args |
|-----|---------|-----------|------|
| `0x01` | Hello / handshake | `00 01` | none |
| `0x15` | Belt / run control | `00 15 <state> <speed> <incline>` | see §4.2 |
| `0x16` | Vibration control | `00 16 <state> <level>` | see §4.3 |
| `0x19` | Status poll (keep-alive) | `00 19` | none |

### 4.1 Hello — `0x01`

Sent once, right after connecting and enabling notifications.

```
00 01                          (inner)
F5 07 00 01 26 D8 FA           (on wire)
```

The device replies with a **hello response** (§5.1) carrying capability/info bytes.

### 4.2 Belt / run control — `0x15`

Inner body: `00 15 <state> <speed> <incline>`

| Arg | Values | Meaning |
|-----|--------|---------|
| `state`   | `0x01` = run, `0x02` = pause, `0x00` = stop | Belt state (see below) |
| `speed`   | `0x00`–…  | **Target speed in km/h × 10** (e.g. `0x1E` = 3.0 km/h). `0x00` when stopping. |
| `incline` | `0x00`–`0x0A` | Incline step (0 = flat, 10 = max; no decline) |

The three `state` values behave differently (confirmed in hass-walkingpad#3):

- **`0x01` run** — start / keep the belt moving at `speed`.
- **`0x02` pause** — belt decelerates to 0 but the device **keeps** its session
  counters (steps / distance / time); a later `run` resumes the session.
- **`0x00` stop** — belt stops **and** the device **resets** the session counters
  to 0 (the next status frame reports all-zero counters).

To **start** the belt and set a speed, send `state=0x01` with the desired `speed`.
The official app ramps the speed up by sending successive `0x15` frames with an
increasing `speed` byte, but a single frame with the final target also works.

To **pause** (keep totals): `00 15 02 00 00`. To **stop** (reset totals): `00 15 00 00 00`.

**Run-control reference frames** (incline `0x00`; generated by the verified encoder,
those overlapping the capture match byte-for-byte):

| km/h | `speed` | On-wire frame |
|------|---------|---------------|
| 0.5 | `0x05` | `F5 0A 00 15 01 05 00 CB 88 FA` |
| 1.0 | `0x0A` | `F5 0A 00 15 01 0A 00 B4 41 FA` |
| 1.5 | `0x0F` | `F5 0A 00 15 01 0F 00 A4 C4 FA` |
| 2.0 | `0x14` | `F5 0A 00 15 01 14 00 05 95 FA` |
| 2.5 | `0x19` | `F5 0A 00 15 01 19 00 1F C9 FA` |
| 3.0 | `0x1E` | `F5 0A 00 15 01 1E 00 6A D9 FA` |
| 3.5 | `0x23` | `F5 0A 00 15 01 23 00 D2 DF FA` |
| 4.0 | `0x28` | `F5 0A 00 15 01 28 00 28 7A FA` |
| 4.5 | `0x2D` | `F5 0B 00 15 01 2D 00 38 F0 0F FA` ¹ |
| 5.0 | `0x32` | `F5 0A 00 15 01 32 00 1C C2 FA` |
| 5.5 | `0x37` | `F5 0A 00 15 01 37 00 0C 47 FA` |
| 6.0 | `0x3C` | `F5 0B 00 15 01 3C 00 F0 06 E2 FA` ¹ |

¹ CRC contains a byte ≥ `0xF0`, so byte-stuffing applies and `LEN` becomes `0x0B`.

**Incline** (at 3.0 km/h), captured:

| Incline | Frame |
|---------|-------|
| 0 | `F5 0A 00 15 01 1E 00 6A D9 FA` |
| 1 | `F5 0A 00 15 01 1E 01 9E B7 FA` |
| 2 | `F5 0A 00 15 01 1E 02 82 04 FA` |

### 4.3 Vibration control — `0x16`

Inner body: `00 16 <state> <level>`

| Arg | Values | Meaning |
|-----|--------|---------|
| `state` | `0x01` = on, `0x00` = off | Vibration state |
| `level` | `0x01`–`0x04` (`0x00` when off) | Vibration intensity |

> The belt and the vibration motor are **mutually exclusive**: enabling vibration
> stops the belt.

Captured frames:

| Action | Frame |
|--------|-------|
| Vibration level 1 | `F5 09 00 16 01 01 55 EA FA` |
| Vibration level 2 | `F5 09 00 16 01 02 49 59 FA` |
| Vibration level 3 | `F5 09 00 16 01 03 BD 37 FA` |
| Vibration level 4 | `F5 09 00 16 01 04 3E 79 FA` |
| Vibration off     | `F5 09 00 16 00 00 34 6D FA` |

### 4.4 Status poll — `0x19`

Inner body: `00 19`. On wire: `F5 08 00 19 F0 0A 59 FA`
(note the CRC low byte `0xFA` is stuffed to `F0 0A`, so `LEN = 0x08`).

Sent repeatedly (~3×/s in the app) to keep the device streaming status
notifications (§5.2).

---

## 5. Notifications (device → app, characteristic `0xFFF1`)

Same envelope. Three message types were observed: `0x01` (hello response),
`0xD0` (command ACK) and `0x19` (status).

### 5.1 Hello response — `0x01`

Sent once after the hello command. Inner body (example):

```
00 01 00 06 00 A8 27 11 01 00 05 78 03 4B 00 0A 01 04 00
```

Carries device capability/config data (units, limits, etc.). Exact field layout is
not yet decoded — not required for control; useful later for exposing device limits.

### 5.2 Status — `0x19`

The core telemetry frame, streamed while polling. Inner body is fixed-layout
(19 bytes). Offsets below are within the **inner body** (`byte[0] = 0x00`,
`byte[1] = 0x19`):

| Offset | Field | Notes |
|-------:|-------|-------|
| 0 | `0x00` | constant channel byte |
| 1 | `0x19` | message type |
| 2–3 | — | constant `00 00` in all samples |
| **4** | **state** | `0x10` = running, `0x0F` = decelerating/stopping, `0x01` = stopped, `0x50` = vibration mode, `0x00` = idle/off |
| 5–7 | — | constant `00 00 00` |
| **8–9** | **duration** | uint16 LE — elapsed seconds |
| **10–11** | **distance** | uint16 LE in **10 m units** → metres = value × 10 |
| **12–13** | calorie-like counter | uint16 LE; slow-moving. Left unmapped — the app's kcal is computed app-side and this field is only a coarse broadcast |
| **14** | **steps** | uint8 (device-confirmed) |
| **15** | **speed** | **current speed in km/h × 10** (`0x1E` = 3.0; ramps down during stop) |
| **16** | **incline** | `0x00`–`0x0A` (0 = flat, 10 = max) |
| 17 | `0x00` | |
| **18** | **vibration level** | `0x00`–`0x04`; retains last level, only meaningful while state (4) is `0x50` |

Field units for duration and distance were derived from a real walk: integrating
speed over the session gives ~30 m and offset 10 reads `3` (× 10 = 30 m), while
offset 8 tracks the elapsed-second count. Steps (offset 14) were confirmed on the
physical console by the device owner (hass-walkingpad#3). The offset 12–13 counter
moves too slowly to be distance and is most likely a coarse calorie estimate; it is
left unmapped pending confirmation.

> Note: steps (offset 14) is a single byte, so it wraps at 255 on this capture's
> layout. Not yet observed on a longer walk — flagged for confirmation.

**Worked examples** (inner body, spaces added):

```
running 3.0 km/h, incline 0:   00 19 00 00 10 00 00 00 10 00 01 00 01 00 0F 1E 00 00 00
running 3.0 km/h, incline 1:   00 19 00 00 10 00 00 00 18 00 01 00 01 00 1C 1E 01 00 00
running 3.0 km/h, incline 2:   00 19 00 00 10 00 00 00 22 00 02 00 02 00 2D 1E 02 00 00
decelerating (speed 2.8):      00 19 00 00 0F 00 00 00 2B 00 03 00 02 00 3D 1C 00 00 00
stopped:                       00 19 00 00 01 00 00 00 2F 00 03 00 02 00 44 00 00 00 00
vibration mode, level 1:       00 19 00 00 50 00 00 00 2F 00 03 00 02 00 44 00 00 00 01
idle / off:                    00 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 04
```

### 5.3 Command ACK — `0xD0`

Immediately after each control command, the device sends a `0xD0` acknowledgement
echoing the command type and its primary argument. Inner body: `00 D0 <CMD> <state> <arg>`.

| After command | ACK inner body |
|---------------|----------------|
| Run (`0x15`, running) | `00 D0 15 01 00` |
| Stop (`0x15`, stop)   | `00 D0 15 02 00` |
| Vibration on (`0x16`) | `00 D0 16 01 00` |
| Vibration off (`0x16`)| `00 D0 16 00 00` |

---

## 6. Example session

Reproduced from the reference capture (times in seconds from start):

| Time | Direction | Meaning | Frame |
|------|-----------|---------|-------|
| 138.6 | → | Hello | `F5 07 00 01 26 D8 FA` |
| 138.8 | ← | Hello response | `F5 19 00 01 …` |
| 146.9 | → | Run, speed 0.2, incline 0 | `F5 0A 00 15 01 02 00 BE 98 FA` |
| …     | → | Speed ramps 0.2 → 3.0 km/h | successive `0x15` frames |
| 167.2 | → | Run, speed 3.0, incline 0 | `F5 0A 00 15 01 1E 00 6A D9 FA` |
| 175.3 | → | Incline 1 | `F5 0A 00 15 01 1E 01 9E B7 FA` |
| 184.8 | → | Incline 2 | `F5 0A 00 15 01 1E 02 82 04 FA` |
| 194.1 | → | Stop belt | `F5 0A 00 15 02 00 00 C3 19 FA` |
| 210.2 | → | Vibration level 1 | `F5 09 00 16 01 01 55 EA FA` |
| 217.7 | → | Vibration level 2 | `F5 09 00 16 01 02 49 59 FA` |
| 225.2 | → | Vibration level 3 | `F5 09 00 16 01 03 BD 37 FA` |
| 231.5 | → | Vibration level 4 | `F5 09 00 16 01 04 3E 79 FA` |
| 238.3 | → | Vibration off | `F5 09 00 16 00 00 34 6D FA` |

Throughout, the app sends the status poll (`F5 08 00 19 F0 0A 59 FA`) ~3×/s and the
device streams `0x19` status notifications.

---

## 7. Open items

- Decode the hello-response (`0x01`) fields → device limits / units.
- Status counters mostly resolved: offset 8–9 = duration (s), 10–11 = distance
  (×10 m), 14 = steps. Offset 12–13 is an unmapped calorie-like counter — confirm
  its unit (or that kcal is purely app-side) with a longer instrumented capture.
- Confirm whether steps (offset 14) is really a single byte (wraps at 255) or has a
  high byte elsewhere — needs a >255-step walk to observe.
- **Max speed is 12.0 km/h** (confirmed by the device owner in hass-walkingpad#3;
  the reference capture only reached 3.0 km/h). The `<speed>` byte still follows
  km/h × 10, so 12.0 km/h = `0x78`.
- **Incline is 0–10, no decline** (confirmed by a full incline-sweep capture in
  hass-walkingpad#3). The `<incline>` byte in the run command and the incline field
  in the status frame both range `0x00` (flat) .. `0x0A` (max); values never go
  negative. The device does not expose a separate decline in the protocol.

---

*Derived from an unfiltered Android HCI snoop capture of a real P3 Max session.
CRC/framing verified against 146/146 distinct captured frames.*
