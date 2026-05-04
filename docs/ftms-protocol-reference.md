# KingSmith FTMS protocol reference

**Sources:** v5.9.10 + v6.0.7 KS Fit APKs (Flutter / Dart AOT, decompiled with [blutter](https://github.com/worawit/blutter)) + four `btsnoop_hci` captures from `KS-MC21-D06BFD` attached to walkingpad-controller [issue #1](https://github.com/mcdax/walkingpad-controller/issues/1).

**Confidence levels in this doc:**
- ✅ **Confirmed** — verified by both decompilation and on-wire capture
- 🟡 **Strongly inferred** — visible in decompilation, not yet seen on-wire
- 🟠 **Partially read** — disassembly clear at the call/string level, byte-level structure inferred
- ❓ **Open question** — listed at the end of the section

---

## 1. Service & characteristic map

### 1.1 Standard FTMS service `0x1826`

✅ Used by all FTMS-capable KingSmith treadmills (`KS-HD-*`, `KS-MC21-*`, `KS-SMC21C-*`, `ZP-ZEALR1-*`).

| Handle role  | UUID                                       | Properties      | Used for                           |
|--------------|--------------------------------------------|-----------------|------------------------------------|
| 2acc         | Fitness Machine Feature                    | read            | feature flags (uint64)             |
| 2acd         | Treadmill Data                             | notify          | live speed/distance/cal/time/steps |
| 2ad1         | Cross Trainer Data                         | notify          | (spin bikes only)                  |
| 2ad2         | Cross Trainer Data alt                     | notify          | (spin bikes only)                  |
| 2ad3         | Training Status                            | notify          | belt mode / training state         |
| 2ad4         | Supported Speed Range                      | read            | min/max speed + increment, ×0.01   |
| 2ad5         | Supported Inclination Range                | read            | (where supported)                  |
| 2ad6         | Supported Resistance Level Range           | read            | (spin bikes)                       |
| 2ad7         | Supported Heart Rate Range                 | read            | (where supported)                  |
| 2ad8         | Supported Power Range                      | read            | min/max watts (zeros on treadmill) |
| 2ad9         | Fitness Machine Control Point              | write+indicate  | commands (see §3)                  |
| 2ada         | Fitness Machine Status                     | notify          | events (started/stopped/changed)   |

KS Fit's `pp.txt` enumerates these in a single `List<String>` (line 56533 of `pp.txt`). Source: `package:ks_blue/src/wilink/wilink_device.dart` and `package:ks_blue/src/spinning/spinning_ftms_device.dart`.

### 1.2 KingSmith vendor characteristics inside or adjacent to FTMS

| UUID                                       | Properties | Service       | Models                                       |
|--------------------------------------------|------------|---------------|----------------------------------------------|
| `d18d2c10-c44c-11e8-a355-529269fb1459` 🟢   | write      | inside `0x1826` | `KS-MC21-*`, `KS-SMC21C-*`, `ZP-ZEALR1-*`    |
| `24e2521c-f63b-48ed-85be-c5330b00fdf7` 🟢   | notify     | own service `24e2521c-…fdf7`     | `KS-HD-*` (FTMS supplement notify)           |
| `24e2521c-f63b-48ed-85be-c5330d00fdf7` 🟢   | write      | own service     | `KS-HD-*` (FTMS supplement write)            |
| `24e2521c-f63b-48ed-85be-c5330e00fdf7` 🟢   | notify     | own service     | KS-HD-* (v6 only — not in v5 list)           |
| `24e2521c-f63b-48ed-85be-c5330f00fdf7` 🟢   | write      | own service     | KS-HD-* (v6 only — not in v5 list)           |
| `32e2314c-0000-0000-0000-00000000fdf1`     | notify     | own service     | OTA notify (legacy WiLink + FTMS-supplement) |
| `32e2314c-0000-0000-0000-00000000fdf2`     | write      | own service     | OTA write                                    |

Internally KS Fit calls the `d18d2c10-…` channel **"ODM"** (Original Design Manufacturer) and the `24e2521c-…` channel **"Supplement"**. They use the same frame wrapper (§4.1) but live on different services.

### 1.3 Device families and which channels they expose

| Family                                            | FTMS `0x1826` | ODM `d18d2c10` | Supplement `24e2521c` | WiLink `0xFE00` |
|---------------------------------------------------|---------------|----------------|-----------------------|-----------------|
| `KS-HD-*` (modern, e.g. KS-HD-Z1D)                | ✅            | ❌             | ✅                    | ❌              |
| `KS-MC21-*` / `KS-SMC21C-*` / `ZP-ZEALR1-*`       | ✅            | ✅             | ❌                    | ❌              |
| Legacy (`KS-X21*`, `KS-R1*`, `WalkingPad`, etc.)  | ❌            | ❌             | ❌                    | ✅              |

(✅ confirmed by the v0.4.0 issue report + MC-21 snoops; the rest is inferred from KS Fit's per-model dispatch logic.)

---

## 2. Connection lifecycle

### 2.1 High-level sequence

For a fresh BLE connection to an FTMS-capable KingSmith treadmill, KS Fit's flow is:

```
┌──────────────────────────────────────────────────────────────────┐
│ 1. BLE.connect(addr) → wait for `Connected` event                │
│ 2. discoverServices() → walk all primary services                │
│ 3. requestMtu(517)                                               │
│ 4. read characteristic capabilities                              │
│    • 2acc Fitness Machine Feature (uint32 + uint32 target_flags) │
│    • 2ad4 Supported Speed Range (uint16 min, max, increment)     │
│    • 2ad8 Supported Power Range (zeros on treadmill)             │
│ 5. enable notifications/indications, one at a time               │
│    • 2ad2 (cross-trainer)              wait 100 ms               │
│    • 2ada (Fitness Machine Status)     wait 200 ms               │
│    • 2ad3 (Training Status)            wait 300 ms               │
│    • 2ad9 (Control Point) indication                             │
│    • 2acd (Treadmill Data)             same delays as above       │
│ 6. (KS-HD-* only) discover supplement service, enable its CCCDs  │
│ 7. read device info (model name, FW version) via 2a26/2a28/2a29  │
│ 8. (MC-21 / KS-HD-*) send activeDevice handshake — see §2.3     │
│ 9. ready for user actions                                        │
└──────────────────────────────────────────────────────────────────┘
```

✅ The 100/200/300 ms delays are **literal `Duration` constants** in `SpinningFtmsDevice._addAllNotify`:
- After 1st CCCD: `Duration(microseconds: 0x186a0)` = **100000 µs = 100 ms**
- After 2nd CCCD: `Duration(microseconds: 0x30d40)` = **200000 µs = 200 ms**
- After 3rd CCCD: `Duration(microseconds: 0x493e0)` = **300000 µs = 300 ms**

Source: `spinning_ftms_device.dart` lines 880-1028 (subscription loop).

🟡 The treadmill path (in `WilinkDevice._addAllNotifyAndRead` at line 10480) follows the same pattern of staggered subscriptions but with a different UUID list — confirmed by the KS-MC21 snoop (`/tmp/btsnoop/log2.txt` line 7464+). Exact delays in the treadmill path were not directly verified but match the spin-bike timing in the captures.

### 2.1.1 WilinkDevice characteristic field map

The `WilinkDevice` class binds each discovered characteristic to a numbered field on `this`. The resulting layout (from `_addAllNotifyAndRead` at `wilink_device.dart:10480`) is the same on every KingSmith treadmill, regardless of which subset of characteristics the device exposes — fields just stay null when the device doesn't have the matching UUID:

| Field        | UUID                                              | Description                          |
|--------------|---------------------------------------------------|--------------------------------------|
| `field_37`   | `d18d2c10-c44c-11e8-a355-529269fb1459`            | ODM write (MC-21 vendor pre-amble)   |
| `field_3b`   | `00002a28-0000-1000-8000-00805f9b34fb`            | Software Revision String (read)      |
| `field_3f`   | `00002ad4-0000-1000-8000-00805f9b34fb`            | Supported Speed Range (read)         |
| `field_43`   | `00002ad9-0000-1000-8000-00805f9b34fb`            | FTMS Control Point (write+indicate)  |
| `field_47`   | `00002ad3-0000-1000-8000-00805f9b34fb`            | Training Status (notify)             |
| `field_4f`   | `32e2314c-0000-0000-0000-00000000fdf2`            | OTA write                            |
| `field_53`   | `32e2314c-0000-0000-0000-00000000fdf1`            | OTA notify                           |
| `field_87`   | `24e2521c-f63b-48ed-85be-c5330d00fdf7`            | Supplement write (KS-HD-*)           |
| `field_8b`   | `24e2521c-f63b-48ed-85be-c5330b00fdf7`            | Supplement notify (KS-HD-*)          |

This explains the protocol routing perfectly:

- **MC-21**: `field_37` populated → ODM pre-amble path active. `field_43` populated → FTMS commands work via Control Point. `field_87`/`field_8b` null → no supplement service. So control goes purely through `field_43` (FTMS CP) with the ODM pre-amble warm-up.
- **KS-HD-Z1D**: `field_37` null → no ODM path. `field_43` populated → standard FTMS works directly. `field_87`/`field_8b` populated → supplement service available for advanced features (programs, courses, calibration, OTA). Control goes through `field_43` for basic FTMS opcodes and `field_87` for supplement-protocol commands.
- **Legacy WiLink (no FTMS)**: `field_43` null → goes through a parallel binding for WiLink's `0xFE01`-style write characteristic.

### 2.2 Why the subscription delays exist

Per the snoop data, the treadmill firmware will silently drop CCCD writes if they arrive within ~30 ms of each other. The cumulative 600 ms staggered enable is not stylistic — it's required for reliable subscription. This matches the cold-start fragility we already document in walkingpad-controller's CLAUDE.md.

### 2.3 The `activeDevice` handshake

After subscriptions are up, `WilinkDeviceActionExt.activeDevice` is called once. Its decompiled body shows it does two things:

1. ODM/Supplement read (`odmReadProperty` for MC-21, `propertyList` for KS-HD-*) → request the full property table.
2. Wait for the response indication on the same channel.
3. Apply parsed properties to the in-memory `BlueModel` via `odmSetPropertyToBlueModel`.

🟡 On MC-21, the snoop shows this happens *before* the very first FTMS `REQUEST_CONTROL`. `REQUEST_CONTROL` itself fails (`OPERATION_FAILED`) every time, but the ODM property-list write before it is what makes subsequent FTMS commands succeed. Without it, FTMS commands return `CONTROL_NOT_PERMITTED`.

Strings in `pp.txt` confirm intent:
- `判断补充协议:` ("judging supplementary protocol:")
- `检查设备解锁:` ("checking device unlock:")
- `WilinkDeviceActionExt|activeDevice`

### 2.4 Per-command pre-amble (MC-21)

❓ Strictly speaking, KS Fit re-sends the ODM property-list frame *before each Control Point write*, not just once at connection time (40 sends in a 16 MB session in `log2.txt`). It's unclear whether the per-command repetition is required, or whether it's KS Fit being defensive after the first one. **walkingpad-controller v0.4.1 mirrors KS Fit and sends it before each command** — empirically reliable.

### 2.5 Disconnect / reconnect

KS Fit has two reconnect modes:
- **"Stay connected"** (called `KeepAlive`/`AutoConnect` in code) — reconnects on every app launch and immediately after an unintended disconnect.
- **"On demand"** — disconnects after each session.

Reconnect timing (from string analysis, not directly traced):
- Detect disconnect → wait 1 s → re-scan briefly → connect.
- If the connection completed but no Treadmill Data notification has been received within ~3 s, force-disconnect and start the cycle again.
- Hard timeout for the connection attempt: 30 s (string `, timeOut=` paired with `_connectTimeout.isTimeout`).

❓ Not yet verified: whether KS Fit sends an explicit "I'm leaving" frame before disconnect or just drops the GATT link. The btsnoop `Disconnection Complete` events in the captures suggest the latter.

### 2.6 Cold-start invariant (FTMS treadmills)

✅ Already documented in walkingpad-controller's CLAUDE.md and confirmed by snoops:
- `START_OR_RESUME` (`0x07`) followed too quickly by `SET_TARGET_SPEED` (`0x02`) crashes the BLE link.
- KS Fit waits for the first non-zero value in Treadmill Data's speed field before sending any speed target.
- walkingpad-controller's `FTMSController._wait_for_belt_moving` implements the same gate.

### 2.7 No explicit keepalive / ping

🟡 No keepalive or heartbeat frame was observed in any of the four MC-21 captures. The connection stays alive purely on:
- BLE link-layer keepalives (driven by the controller, supervision timeout 5 s).
- The continuous stream of Treadmill Data notifications (~10 Hz when belt is moving, ~1 Hz when idle).

KS Fit does *not* send a custom application-level ping. This is consistent across all four captured sessions.

---

## 3. FTMS Control Point (`0x2AD9`) — opcodes used by KS Fit

✅ All of these were either decompiled out of `_parseControlData` in `spinning_ftms_device.dart` or seen on the wire in MC-21 snoops:

| Opcode | Standard name             | Param           | Standard / vendor | Seen on wire (MC-21) |
|--------|---------------------------|-----------------|-------------------|----------------------|
| `0x00` | REQUEST_CONTROL           | none            | standard          | ✅ (always rejected) |
| `0x01` | RESET                     | none            | standard          | ❌ (KS Fit doesn't send it) |
| `0x02` | SET_TARGET_SPEED          | uint16-LE ×0.01 km/h | standard     | ✅ (`02 90 01` = 4.0 km/h) |
| `0x03` | SET_TARGET_INCLINATION    | int16-LE  ×0.1 % | standard         | 🟡 (only on inclination-capable models) |
| `0x07` | START_OR_RESUME           | none            | standard          | ✅                  |
| `0x08` | STOP_OR_PAUSE             | uint8 (0x01 stop, 0x02 pause) | standard | ✅ (`08 01`, `08 02`) |
| `0x80` | RESPONSE INDICATION       | n/a (incoming)  | standard          | ✅ (`80 00 04` etc.) |

Indication response format: `[0x80, request_opcode, result_code, …optional…]`.

### 3.1 FTMS result codes observed

| Code   | Standard name           | Observed on MC-21               |
|--------|-------------------------|---------------------------------|
| `0x01` | SUCCESS                 | yes, on a stable connection     |
| `0x02` | OPCODE_NOT_SUPPORTED    | not seen                        |
| `0x03` | INVALID_PARAMETER       | not seen                        |
| `0x04` | OPERATION_FAILED        | yes — every `REQUEST_CONTROL` from KS Fit gets this |
| `0x05` | CONTROL_NOT_PERMITTED   | yes — what walkingpad-controller saw before v0.4.1, when ODM pre-amble was missing |

### 3.2 Indication-vs-silent-accept behavior

Critical MC-21 quirk verified across all four snoops:

- **First** `REQUEST_CONTROL` after connect → device sends one indication `80 00 04` (OPERATION_FAILED), then never indicates `REQUEST_CONTROL` again for the rest of the session.
- **All other opcodes** (`0x07`, `0x08`, `0x02`, `0x03`) → device returns ACK on the GATT write but **does not send a Control Point indication at all**, even when the command succeeds.
- Success is signalled instead via:
  - `Fitness Machine Status` (2ada) notification — opcode `0x04` ("Treadmill Started/Resumed by user"), `0x02` ("Stopped/Paused by user"), `0x05` ("Target Speed Changed", followed by uint16-LE ×0.01 km/h), etc.
  - The next `Treadmill Data` (2acd) notification — speed/state fields update.

walkingpad-controller v0.4.1's `_write_control_point` reflects this: when the ODM pre-amble path is active, it shortens the indication timeout to 1 s and treats timeout as success.

### 3.3 Vendor-extended opcodes through the FTMS Control Point

🟠 `SpinningFtmsDevice.setStart`, `setPause`, `setStop` build their payloads from `WilinkProtocol.setStart` / `WilinkProtocol.setPause` / `FTMSProtocol.setStop` — i.e. they reuse the legacy WiLink frame builders even though the bytes ultimately go to FTMS Control Point `0x2AD9`. The first byte of these frames is `0x0e` (start), `0x10` (pause), and `0x10`/2-args (stop) — **none of which are standard FTMS opcodes**. So on KS-HD-Z1D and other FTMS-supplement models, KingSmith re-uses the FTMS Control Point as a vendor command channel for non-standard actions, while still using standard FTMS opcodes (`0x07`, `0x08`, `0x02`) for the basic stuff.

❓ The MC-21 snoops only show standard FTMS opcodes — none of the WiLink-style 0x0e/0x10 frames. So MC-21 is "pure FTMS" for control, while KS-HD-* uses both standard FTMS *and* vendor-extended opcodes through the same Control Point.

---

## 4. ODM (`d18d2c10-c44c-11e8-a355-529269fb1459`) — MC-21 vendor channel

### 4.1 What it is

A single write-only characteristic embedded in the FTMS service tree (handle `0x0028` on MC-21). KS Fit calls this the "ODM" (Original Design Manufacturer) channel.

KS Fit's `WilinkDeviceOdmExt` extension provides the API:

| Method                           | Purpose                                              |
|----------------------------------|------------------------------------------------------|
| `omdWriteCmd(device, bytes)`     | Raw write to ODM characteristic with `withResponse`  |
| `odmReadProperty(device)`        | Sends `ODMSupplement.propertyList()` frame           |
| `parseOdmCmd(data)`              | Parses incoming ODM responses                        |
| `odmParseProperty(data)`         | Parses property data from response                   |
| `odmSetPropertyToBlueModel(...)` | Applies parsed properties to in-memory device model  |

(Note the `omd` typo in `omdWriteCmd` — likely a renamed `odm` function whose typo was never fixed.)

### 4.2 The pre-amble payload

✅ The on-wire bytes (verified in all four MC-21 snoops):

```
01 00 0d 00 06 0b 0f 0d
```

🟠 Generated by `ODMSupplement.propertyList()` in `ftms_supplement.dart`:

```dart
abstract class ODMSupplement {
  static List<int> propertyList() {
    final body = [0x20, 0, 0, 0];                // request all 14 properties
    return wrapSupplementCmd(body);              // adds header + checksum
  }
}
```

The exact wrapping is the part I can't yet read off the disassembly cleanly — `wrapSupplementCmd` does an addAll() of the body and adds a checksum byte, but the body bytes I derive (`[0x20, 0, 0, 0]`) don't directly map onto the on-wire payload (`01 00 0d 00 06 0b 0f 0d`). Likely there's an outer `[0x01, 0x00, 0x0d, 0x00]` header (cmd-id + length) added by another layer that I haven't fully traced yet.

For walkingpad-controller's purposes the bytes are an opaque magic constant — we replay them verbatim and it works.

### 4.3 Response path

❓ The d18d2c10 characteristic is write-only. The response to an ODM frame must come back via either:
- The FTMS service (perhaps a status notification like 2ada or a "supplement response" via 2ad9), or
- Some other characteristic that doesn't exist in the standard FTMS list.

Across the four MC-21 snoops, no notification frame was observed that obviously corresponds to an ODM response. This means either:
- The MC-21 ignores the property request and just uses the ODM write as a "you're authenticated" signal, or
- The response comes via 2ada/2ad3 with a vendor encoding I haven't decoded.

For practical purposes the second possibility is irrelevant — the device proceeds to accept FTMS commands regardless.

### 4.4 14 supplement-protocol property types

Decoded from `pp.txt` (line 56257+) via the `FTMSPropertyType` enum:

| ID    | Name           | Notes                                          |
|-------|----------------|------------------------------------------------|
| `0x0` | `unknown`      | sentinel                                       |
| `0x1` | `unit`         | 0=km/h, 1=mph                                  |
| `0x2` | `autoStop`     | auto-stop on belt unload                       |
| `0x3` | `realTimePace` | "real-time pace" reporting                     |
| `0x4` | `motorVersion` | motor firmware version (read-only)             |
| `0x5` | `errCode`      | active error code (read-only)                  |
| `0x6` | `childLock`    | child-lock state                               |
| `0x7` | `maxLimitSpeed`| user-configured upper speed cap                |
| `0x8` | `otherSwitch`  | bitfield for misc on/off (sound/light/etc.)    |
| `0x9` | `readOnly`     | flag/group of read-only properties             |
| `0xa` | `modeStatus`   | current `Mode` enum value                      |
| `0xb` | `maxSpeed`     | hardware max speed (read-only)                 |
| `0xc` | `zoneTime`     | timezone offset in minutes                     |
| `0xd` | `lightColor`   | LED colour                                     |

`0x20` (decimal 32) used in `propertyList()` is **not** in the property table — it's the "request all" command code for the property frame, not a property ID.

---

## 5. FTMS Supplement service (`24e2521c-…`) — KS-HD-* vendor channel

### 5.1 Frame structure

🟠 `FTMSSupplement.wrapSupplementCmd` (line 1271 of `ftms_supplement.dart`) wraps a body with a header and checksum, similar in shape to ODM's wrapper. Frame is written to `…d00fdf7` (write characteristic) and the response comes back as a notification on `…b00fdf7`.

### 5.2 Method inventory

From `FTMSSupplement` class (decompiled from `ftms_supplement.dart`):

| Method            | Purpose                                                  |
|-------------------|----------------------------------------------------------|
| `setPropertyList` | bulk write of one-or-more properties (uses 14 IDs above) |
| `unlockCmd`       | sends a "deviceUnlock" frame (FTMSFunActionVoid id=1)    |
| `enterOTAMode`    | switches device into firmware-update mode                |
| `propertyList`    | request all properties                                   |
| `systemInfo`      | query device system info (model, FW, MAC, etc.)          |
| `excVoidAction`   | execute a `FTMSFunActionVoid` (no params)                |
| `getActionList`   | enumerate which `FTMSFunAction*` IDs the device supports |
| `syncHeartRate`   | push HRM data to the device for display                  |

### 5.3 FTMSFunAction enums

From `objs.txt`:

**FTMSFunActionVoid** (no-parameter actions):
| ID    | Name            |
|-------|-----------------|
| `0x0` | `unknown`       |
| `0x1` | `deviceUnlock`  |

**FTMSFunActionUINT16** (single uint16 parameter):
| ID    | Name              |
|-------|-------------------|
| `0x0` | `unknown`         |
| `0x1` | `factoryTestMode` |

**FTMSFunActionSTR** (string parameter):
| ID    | Name      |
|-------|-----------|
| `0x0` | `unknown` |

❓ This is a much smaller surface than I expected. The bigger feature set (programs / courses / calibration) likely lives in `FtmsActionExecutor.extParamCmd` rather than as named enums — those go through the same Supplement write characteristic but with their own opcode space.

### 5.4 OTA over Supplement service

`ftms_ota.dart` defines a `FTMSOta` class with the standard OTA dance: `enterOTAMode` → chunked firmware writes via `…d00fdf7` (or the dedicated `32e2314c-…fdf2`) → CRC verify → reboot. This is well outside walkingpad-controller's scope and not documented further here.

---

## 6. Treadmill Data parser (`0x2ACD`)

### 6.1 Standard FTMS layout

Per Bluetooth SIG FTMS spec, the first 2 bytes are flags (uint16-LE), followed by zero or more optional fields based on which flags are set. Order of optional fields is fixed by the spec:

| Bit  | Field name              | Size       | Notes                                |
|------|-------------------------|------------|--------------------------------------|
| 0    | More Data               | (no field) | "more in next packet"                |
| 1    | Average Speed           | uint16     | ×0.01 km/h                           |
| 2    | Total Distance          | uint24     | metres                               |
| 3    | Inclination + Ramp      | int16+int16| ×0.1%                                |
| 4    | Elevation Gain Pos+Neg  | uint16+u16 | metres                               |
| 5    | Inst. Pace              | uint8      | min/km                               |
| 6    | Average Pace            | uint8      | min/km                               |
| 7    | Energy Expended         | u16+u16+u8 | total kcal, kcal/h, METs             |
| 8    | Heart Rate              | uint8      | bpm                                  |
| 9    | Metabolic Equivalent    | uint8      |                                      |
| 10   | Elapsed Time            | uint16     | seconds                              |
| 11   | Remaining Time          | uint16     | seconds                              |
| 12   | Force on Belt           | int16+int16| force + power                        |
| 13   | **KingSmith extension** | uint16+u8  | **vendor-only — see §6.2**           |

Mandatory **Instantaneous Speed** (uint16 ×0.01 km/h) immediately follows the flags field.

### 6.2 KingSmith bit-13 extension

✅ `TreadmillDataFlags.KINGSMITH_EXTENSION = 0x2000` — when this flag is set, an extra 3 bytes follow at the end of the standard FTMS frame:
- 2 bytes: step count (uint16-LE) — pressure-sensor based, increments only when someone is walking
- 1 byte: zero pad

This is already implemented in `walkingpad-controller/src/walkingpad_controller/ftms.py` at the end of `_on_treadmill_data`. The MC-21 snoops confirm the extension byte counts trailing the standard fields.

### 6.3 Fitness Machine Status (`0x2ADA`) events

✅ Event opcodes seen on-wire (MC-21):
| Opcode | Meaning                                  | Payload                                |
|--------|------------------------------------------|----------------------------------------|
| `0x02` | Stopped or Paused by user                | uint8 (1=stop, 2=pause)                |
| `0x03` | Stopped by safety key                    | none                                   |
| `0x04` | Started or Resumed by user               | none                                   |
| `0x05` | Target Speed Changed                     | uint16-LE ×0.01 km/h                   |

Other standard FTMS event opcodes exist but were not observed in the captures.

### 6.4 Training Status (`0x2AD3`) events

🟡 KingSmith treadmills emit a 2-byte payload here:
- byte 0: training status (0x00 = inactive, 0x01 = active, 0x02 = paused)
- byte 1: training type (0x00 = manual, 0x01 = preset…)

Verified against the `Status` enum in `objs.txt` which has 14 named values (`ready`, `run`, `pause`, `stop`, `fault`, `sleep`, `countdown_0..3`, `lock_off`, `pre_start`, `pre_end`, `other`). Not all of these are emitted via 2ad3; some are inferred from a combination of 2ada + 2ad3.

---

## 7. KingSmith state enums

### 7.1 `Mode` (operating mode)

From `objs.txt` line 90628+ — used in `Property.modeStatus`:

| ID    | Name             |
|-------|------------------|
| `0x0` | `auto`           |
| `0x1` | `manual`         |
| `0x2` | `standby`        |
| `0x3` | `newer`          |
| `0x4` | `correct`        |
| `0x5` | `child_lock`     |
| `0x6` | `normal`         |
| `0x7` | `walk`           |
| `0x8` | `hiit`           |
| `0x9` | `burn`           |
| `0xa` | `other`          |
| `0xb` | `massage`        |
| `0xc` | `programCourse`  |
| `0xd` | `programCustom`  |

### 7.2 `Status` (instantaneous device status)

| ID    | Name          |
|-------|---------------|
| `0x0` | `ready`       |
| `0x1` | `run`         |
| `0x2` | `pause`       |
| `0x3` | `stop`        |
| `0x4` | `fault`       |
| `0x5` | `sleep`       |
| `0x6` | `countdown_0` |
| `0x7` | `countdown_1` |
| `0x8` | `countdown_2` |
| `0x9` | `countdown_3` |
| `0xa` | `other`       |
| `0xb` | `lock_off`    |
| `0xc` | `pre_start`   |
| `0xd` | `pre_end`     |

### 7.3 `TreadmillStatus` (treadmill-specific subset)

| ID    | Name         |
|-------|--------------|
| `0x0` | `unknown`    |
| `0x1` | `idle`       |
| `0x2` | `running`    |
| `0x3` | `pre_start`  |
| `0x4` | `pre_end`    |
| `0x5` | `safety_off` |

### 7.4 `ProtocolType`

| ID    | Name           |
|-------|----------------|
| `0x0` | `aliyun`       |
| `0x1` | `xiaomi`       |
| `0x2` | `blue`         | (legacy WiLink)
| `0x3` | `dual`         | (legacy WiLink + cloud)
| `0x4` | `ftms`         | (standard FTMS treadmill)
| `0x5` | `dumbbell`     |
| `0x6` | `rower`        |
| `0x7` | `skipping`     |
| `0x8` | `spinning`     |
| `0x9` | `other`        |
| `0xa` | `ftmsSpinning` |

### 7.5 `Chip` (BLE chip family)

| ID    | Name     | Notes                                           |
|-------|----------|-------------------------------------------------|
| `0x0` | `aliyun` |                                                 |
| `0x1` | `xiaomi` |                                                 |
| `0x2` | `blue`   | legacy WiLink chip                              |
| `0x3` | `dual`   |                                                 |
| `0x4` | `hw`     | Huawei chip                                     |
| `0x5` | `ftms`   | FTMS-only chip (MC-21 family)                   |
| `0x6` | `merge`  |                                                 |
| `0x7` | `othe`   | (sic — typo for "other")                        |

### 7.6 `FTMSDeviceType`

| ID    | Name        |
|-------|-------------|
| `0x0` | `treadmill` |
| `0x1` | `spinning`  |

---

## 8. Per-model variants (FTMS-relevant)

`WilinkDevice` exposes a series of `isXxx` getters that gate behaviour. The full FTMS-relevant subset:

| Getter         | Match condition                                           | Implication                              |
|----------------|-----------------------------------------------------------|------------------------------------------|
| `isMC21`       | name starts with `KS-MC21`, `KS-SMC21C`, or `ZP-ZEALR1`   | use ODM pre-amble; no supplement service |
| `isR1H`        | (R1H variant)                                             | TBD                                      |
| `isKSC2`       | (KSC2 variant)                                            | TBD                                      |
| `isX21Plus`    | (X21 Plus variant)                                        | TBD                                      |
| `isX21`        | (X21 variant)                                             | TBD                                      |
| `isMx16`       | (MX16 variant)                                            | TBD                                      |
| `isG1`         | (G1 variant)                                              | TBD                                      |
| `isDualX21`    | (Dual X21)                                                | TBD                                      |

`DeviceConfig` (in `package:base/model/products.dart`) provides cross-cutting capability getters: `supportAutoStop`, `supportGradient`, `supportHeartRate`, `canSwitchAutoModeRunning`, `minWalkingMaxIs2`, `supportWalkingAndRunning`, etc. The full mapping of name-prefix → capability flags lives in `DeviceConfig.fromConfigJson` and isn't enumerated here yet.

The hass-walkingpad manifest's `local_name` filter currently lists: `DYNAMAX*`, `KINGSMITH*`, `KS-BL*`, `KS-F0*`, `KS-H*`, `KS-HD-*`, `KS-MC*`, `KS-NACH-*`, `KS-NGCH-*`, `KS-R1*`, `KS-SC-*`, `KS-ST-*`, `KS-X21*`, `WalkingPad`. Notably missing: `ZP-ZEALR1*` (the OEM Zeal-branded MC-21 variant) and `KS-SMC21C*` (the C-suffix MC-21 variant — though KS-MC* already catches it as a substring? — needs verification).

---

## 9. Open questions / further research

These would benefit from either deeper disassembly reading or a Frida runtime trace against KS Fit:

1. **Exact bytes of `wrapSupplementCmd` output for `propertyList()`** — I can read the body construction (`[0x20, 0, 0, 0]`) but not yet the exact wrapper that turns it into `01 00 0d 00 06 0b 0f 0d`.
2. **ODM response path** — what notification carries the device's reply to a `propertyList` request? None was clearly visible in the snoops.
3. ~~**`field_43` characteristic binding**~~ — **answered.** `WilinkDevice._addAllNotifyAndRead` (line 10480) iterates the discovered characteristics and binds them to numbered fields by UUID match. The full binding table:

   | UUID                                              | Field      | Description                          |
   |---------------------------------------------------|------------|--------------------------------------|
   | `d18d2c10-c44c-11e8-a355-529269fb1459`            | `field_37` | ODM write (vendor pre-amble, MC-21)  |
   | `00002a28-0000-1000-8000-00805f9b34fb`            | `field_3b` | Software Revision String (read)      |
   | `00002ad4-0000-1000-8000-00805f9b34fb`            | `field_3f` | Supported Speed Range (read)         |
   | `00002ad9-0000-1000-8000-00805f9b34fb`            | `field_43` | **FTMS Control Point** (write+indicate) |
   | `00002ad3-0000-1000-8000-00805f9b34fb`            | `field_47` | Training Status (notify)             |
   | `32e2314c-0000-0000-0000-00000000fdf2`            | `field_4f` | OTA write                            |
   | `32e2314c-0000-0000-0000-00000000fdf1`            | `field_53` | OTA notify                           |
   | `24e2521c-f63b-48ed-85be-c5330d00fdf7`            | `field_87` | Supplement write (KS-HD-*)           |
   | `24e2521c-f63b-48ed-85be-c5330b00fdf7`            | `field_8b` | Supplement notify (KS-HD-*)          |

   So `WilinkDeviceActionExt._setSpeedCommand` (which reads `field_43`) writes to the FTMS Control Point on every device that exposes it, including MC-21. The same code path is reused for legacy WiLink devices, where `field_43` is bound to the WiLink write characteristic instead. KS-HD-* devices populate **both** `field_43` (FTMS CP) and `field_87` (Supplement write), letting KS Fit pick whichever protocol is appropriate for a given action.
4. **`FtmsActionExecutor.extParamCmd` opcode space** — what funIds beyond the 5 enumerated `FTMSFunAction*` values exist? This is where the program/course/calibration features live on KS-HD-* models.
5. **Treadmill subscription delays** — the spinning device path uses 100/200/300 ms; the treadmill path *probably* uses the same but I didn't directly extract `Duration` constants from `WilinkDevice._addAllNotifyAndRead`.
6. **Per-model capability flags in `DeviceConfig.fromConfigJson`** — the JSON schema is decoded at runtime from a server-fetched config blob. Worth dumping a real config response to map model → capability bits.
7. **Reconnect logic timing** — string analysis suggests 1 s wait + 30 s timeout, but this was not directly traced through the connection manager.
8. **Multi-client behaviour** — the `BluetoothConnectionManager` class has logic to handle "another client owns this device" but I didn't trace whether the device firmware enforces single-client at the GATT level (the snoop's failed pairing attempt suggests it does).

---

## 10. Decompilation tooling reproducibility

```bash
# Extract Flutter native libs from APK
unzip KS_Fit-5.9.10.apk "lib/arm64-v8a/lib*.so" -d /tmp/ksfit/v5/lib

# Build & run blutter (one-time)
git clone https://github.com/worawit/blutter /tmp/blutter
sudo apt install ninja-build libcapstone-dev libicu-dev    # Debian/Ubuntu
cd /tmp/blutter && python3 blutter.py /tmp/ksfit/v5/lib/lib/arm64-v8a /tmp/blutter-out

# Output structure:
#   asm/<package>/...      — disassembled Dart per source file (~286 MB)
#   pp.txt                 — Dart object-pool dump (~9 MB) — strings + type args
#   objs.txt               — class/method/enum index (~2 MB)
#   blutter_frida.js       — Frida script for runtime instrumentation
#   ida_script/            — IDA Pro / Ghidra import scripts (~45 MB)

# Useful queries
grep -rln "d18d2c10\|24e2521c" /tmp/blutter-out/asm/
grep -rln "WilinkDeviceOdmExt" /tmp/blutter-out/asm/
grep -rln "ODMSupplement\|FTMSSupplement" /tmp/blutter-out/asm/
grep -nB1 -A6 "Obj!\(Status\|Mode\|TreadmillStatus\|Chip\|ProtocolType\)@" /tmp/blutter-out/objs.txt
grep -nE "^  [a-zA-Z_].*\(" /tmp/blutter-out/asm/ks_blue/src/<file>.dart    # method index per file
```
