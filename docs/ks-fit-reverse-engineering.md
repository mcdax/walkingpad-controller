# KS Fit reverse-engineering notes

This is a research log produced from the v5.9.10 (`KS_Fit-5.9.10.apk`) and v6.0.7 APKs. KS Fit is a Flutter app — the Java/Dex layer is just a wrapper, all the protocol logic lives in `lib/<arch>/libapp.so` as a Dart AOT snapshot.

The Dart code was extracted with **blutter** (https://github.com/worawit/blutter), targeting the v5.9.10 arm64 build. Output is roughly 286 MB of disassembled Dart bytecode plus ~9 MB of object-pool dumps; the load-bearing portion is the `ks_blue` package (~40k lines of decompiled Dart).

> Caveat: blutter does not produce real Dart source — it produces structured ARM disassembly with Dart class/method/string symbols recovered. Function bodies below are best-effort transcriptions of intent, not a literal source listing. Where exact byte-level encoding matters, cross-check with HCI snoops.

## Source layout

The KingSmith Bluetooth code is organised as a Dart package called **`ks_blue`**:

```
package:ks_blue/
├── ks_blue.dart, ks_blue_manager.dart
├── src/
│   ├── ble/                         # BLE plumbing (single & dual)
│   │   ├── blue_single_module.dart
│   │   ├── parser.dart, parser_bt.dart
│   │   └── protocol.dart
│   ├── wilink/                      # legacy WiLink treadmill protocol
│   │   ├── wilink_device.dart       # 18k lines — WilinkDevice + extensions
│   │   ├── wilink_protocol.dart     # frame builders (setSpeed, setStart, …)
│   │   ├── ftms_action_executor.dart
│   │   ├── ftms_supplement.dart     # supplement service (24e2521c-…) + ODM
│   │   ├── ftms_ota.dart            # OTA over supplement service
│   │   ├── treadmill_data.dart, property.dart
│   ├── spinning/
│   │   ├── spinning_device.dart, spinning_ftms_device.dart
│   ├── rower/
│   │   ├── ftms_protocol.dart       # FTMS frame builders, used by spinning_ftms
│   ├── dual/, dumbbell/, skipping/, rower/, ota/, util/, base/, extension/
```

The treadmill / spinning devices ultimately use mixins from `package:sport/sport_manager/device/abstract_ble_device.dart`:

```dart
abstract class TreadmillDevice extends Object { ... }
abstract class SportDevice extends Object { ... }
abstract class BleDevice extends BaseDevice { ... }
abstract class KsTreadmillDevice extends _KsTreadmillDevice & BleDevice & SportDevice & TreadmillDevice { ... }

class WilinkBleDevice extends KsTreadmillDevice { ... }
class SpinningFtmsDevice extends GenericDevice { ... }
```

Notably **there is no `FtmsTreadmillDevice` class.** All KingSmith treadmills (legacy WiLink and pure-FTMS like the MC-21) are instantiated as `WilinkBleDevice`. The decision of *which* characteristic to write to is made dynamically inside `WilinkDeviceActionExt` based on which characteristics the device exposes — see "Speed control flow" below.

## Model detection

`WilinkDevice` exposes a number of model-flag getters (`get isMC21`, `get isR1H`, `get isX21`, `get isKSC2`, `get isMx16`, `get isG1`, `get isDualX21`, …). Each is a name-prefix check against the device's BLE name.

The `isMC21` getter (decompiled):

```dart
bool get isMC21 {
  final name = BluetoothDeviceExt.deviceName(this);
  return name.startsWith("ZP-ZEALR1")
      || name.startsWith("KS-MC21")
      || name.startsWith("KS-SMC21C");
}
```

Three prefixes match the MC-21 family:

| Prefix       | Notes                                              |
| ------------ | -------------------------------------------------- |
| `KS-MC21`    | Standard KingSmith retail SKU (e.g. `KS-MC21-D06BFD` from issue #1) |
| `KS-SMC21C`  | "C" variant — different OEM run                    |
| `ZP-ZEALR1`  | Zeal-branded OEM variant                           |

Also seen elsewhere in strings: `HW-KS-HC-MC21A` (hardware identifier reported by the device). Other KingSmith codes confirmed in `ProductFunMenus`/`DeviceConfig`: `KS-X21*`, `KS-R1*`, `KS-HD-*`, `KS-NACH-*`, `KS-NGCH-*`, `KS-BL*`, `KS-F0*`, `KS-H*`, `KS-SC-*`, `KS-ST-*`, `DYNAMAX*`, `KINGSMITH*`, `WalkingPad`. (The hass-walkingpad manifest already covers all of these.)

## The d18d2c10 vendor characteristic — internally called "ODM"

The mystery characteristic from issue #1 is the **ODM** (Original Design Manufacturer) channel. KS Fit code has a dedicated extension `WilinkDeviceOdmExt` on `WilinkDevice` with these methods:

| Method                           | Purpose |
| -------------------------------- | ------- |
| `omdWriteCmd(device, bytes)`     | Raw write to the ODM characteristic |
| `odmReadProperty(device)`        | Sends a "request property list" frame (this is the magic 8-byte payload from the snoops) |
| `parseOdmCmd(data)`              | Parses incoming ODM responses |
| `odmParseProperty(data)`         | Parses property data from an ODM response |
| `odmSetPropertyToBlueModel(...)` | Applies parsed properties to the in-memory device model |

There are also three Chinese log strings worth flagging because they confirm intent:

- `判断补充协议:` ("judging supplementary protocol:")
- `检查设备解锁:` ("checking device unlock:")
- `odm write: …`, `odm write error: …`, `odm write timeout`

So this isn't an opportunistic write to a random characteristic — KS Fit's authors think of it as part of an "unlock" / "supplementary protocol" handshake.

### How the magic bytes are produced

The on-wire payload `01 00 0d 00 06 0b 0f 0d` is constructed by `ODMSupplement.propertyList()` in `ftms_supplement.dart`. Pseudocode:

```dart
abstract class ODMSupplement {
  static List<int> propertyList() {
    final input = <int>[0x20, 0, 0, 0];     // request all properties
    return wrapSupplementCmd(input);         // adds header + checksum
  }

  static List<int> wrapSupplementCmd(List<int> body) {
    int sum = 0;
    for (var b in body) sum += b;
    final out = <int>[];
    out.addAll(body);
    out.add(sum & 0xff);                    // checksum byte
    return out;
  }
}
```

The wrapping I read off the disassembly produces a 5-byte buffer (`body + checksum`), but the on-wire frame is 8 bytes. Two plausible explanations:

1. There's an outer header (`01 00 0d 00`, looks like a fixed 4-byte preamble — possibly cmd-id + length) added by another layer I didn't fully chase down. In that reading, the on-wire payload is `[0x01, 0x00, 0x0d, 0x00, 0x06, 0x0b, 0x0f, 0x0d]` = `[header(4), body(3 zeros), checksum(0x0d)]` — but the body bytes don't quite match what `propertyList()` builds either, so I'm not 100% certain.
2. My reading of `wrapSupplementCmd` is incomplete and the actual function does more than the loop suggests.

For practical purposes this doesn't matter — the bytes are a fixed value KS Fit replays unchanged before each Control Point command, and the library does the same.

The same wrapping function is used by `FTMSSupplement` for the standard supplement service (`24e2521c-…`) on KS-HD-* devices.

### Why does it appear to "unlock" the FTMS Control Point?

Working theory based on the logs and decompilation: the device firmware does *not* require the ODM frame for FTMS to work in general. What the firmware *does* care about is that `propertyList()` is read first — it asks the device for its property metadata (capabilities, model, supported sport types, etc.) and the response populates internal state on the firmware that subsequent FTMS writes depend on. In particular, the `ODMSupplement.propertyList()` request acts as a "you've identified yourself" handshake — without it, the device returns `CONTROL_NOT_PERMITTED` to FTMS writes that would otherwise succeed.

This is consistent with the snoop evidence: KS Fit *does* send the ODM frame before each user action, even on the very first command after connect.

## Speed control flow

This was the original mystery from issue #1 (does KS Fit ever send FTMS `SET_TARGET_SPEED`?). The decompilation says: yes, but the routing is non-obvious.

The high-level call chain on the treadmill side:

```
WilinkBleDevice.setSpeed(speedKmh)
  → WilinkDeviceActionExt.setSpeed(_winlinkDevice, speedKmh)
    → _setSpeedCommand(_winlinkDevice)
      → bytes = WilinkProtocol.setSpeed(speedKmh)
      → GenericDevice.write(field_43, bytes, withoutResponse)
```

`WilinkProtocol.setSpeed` builds a **6-byte buffer** with `(speedKmh * 10)` rounded to int and split across two bytes:

```dart
List<int> setSpeed(double speedKmh) {
  final raw = (speedKmh * 10).round();        // 0.1 km/h precision
  final lo = raw & 0xff;
  final hi = (raw >> 8) & 0xff;
  final bytes = Uint8List(4);
  bytes[0] = hi;
  bytes[3] = lo;
  return bytes;                                // ⚠ exact field positions tentative
}
```

Critically, **the destination characteristic is `field_43`, not the WiLink write characteristic literally.** `field_43` is set during connection setup based on what the device exposes. On a legacy WiLink device, it's the `0xFE00`-service write characteristic. On a pure-FTMS device like the MC-21, the connection setup almost certainly binds `field_43` to the FTMS Control Point (`0x2AD9`).

That's the open question. I confirmed via snoop that KS Fit on MC-21 writes FTMS-formatted payloads (`02 90 01` for SET_TARGET_SPEED at 4.0 km/h) to the Control Point, but I did not fully trace `_addAllNotifyAndRead`'s logic to confirm which characteristic gets bound to `field_43` for MC-21. To do that cleanly we'd need to either run blutter's Frida script against a live KS Fit + MC-21 session, or read further into `_addAllNotifyAndRead` (the `d18d2c10`-detection function in `wilink_device.dart` lines ~10588–10700, which has many branches per characteristic UUID).

Either way: the on-wire encoding for SET_TARGET_SPEED on MC-21 is *standard FTMS* (`opcode=0x02 || UINT16-LE(speed*100)`), confirmed by the snoop, so this library's existing `FTMSController.set_target_speed` is correct. The fix in v0.4.1 (vendor pre-amble + tolerate REQUEST_CONTROL rejection) is the right shape.

## What we already replicate correctly in walkingpad-controller v0.4.1

After this analysis, the v0.4.1 fixes look right:

- ✅ Detect the vendor pre-amble characteristic (`d18d2c10-…`) at connection time and write the magic 8-byte payload before each Control Point command. Matches what KS Fit's `WilinkDeviceOdmExt.odmReadProperty` does.
- ✅ Tolerate `REQUEST_CONTROL` rejection. KS Fit does the same — it ignores the `OPERATION_FAILED` indication and proceeds with `START_OR_RESUME` / `SET_TARGET_SPEED` / `STOP_OR_PAUSE`.
- ✅ Treat indication-timeout as success on the pre-amble path. The MC-21 firmware silently accepts most commands without sending an FTMS indication response; KS Fit ignores the missing response, and so do we.

## What we *don't* yet replicate (and probably should consider)

These are surfaced by the decompilation but not currently in walkingpad-controller:

1. **Parse and use the property-list response.** When KS Fit sends `propertyList()`, it expects a response that carries the device's actual capabilities (supported speed range, supported features, model identification, etc.). `WilinkDeviceOdmExt.parseOdmCmd` + `odmParseProperty` + `odmSetPropertyToBlueModel` decode that and update the in-memory model. We currently just do a fire-and-forget write and read FTMS `Supported Speed Range` (`0x2ad4`) directly — which works, but on some firmware revisions might be less accurate than the ODM property table.

2. **The supplement service (`24e2521c-…`) on KS-HD-* devices.** `FTMSSupplement` exposes a wider protocol: `setPropertyList`, `setUser`, `setUnit`, `getOfflineRecord`, OTA, presets, courses. The MC-21 doesn't have this service, but devices like `KS-HD-Z1D` do. This is the gap that lets the official app do things our library can't (custom programs, calibration, OTA).

3. **WiLink protocol implementation.** Right now we delegate WiLink to the optional `ph4-walkingpad` dependency. With the decompiled `WilinkProtocol` source we could implement WiLink natively and drop the optional dep. Several methods are easy: `setSpeed`, `setStart`, `setPause`, `setMode`, `setLightSwitch`, `setSoundSwitch`, `setChildLock`, `setUnit`, `setMaxSpeed`, `setInclination`, `syncHeartRate`, `setAutoStop`. There are also extensive Read methods (`getSpeed`, `getMaxSpeed`, `getMinSpeed`, `getSpeedStep`, `getSpeedRange`, `currentSpeed`, etc.) — same WiLink wire format on the read side.

4. **`FtmsActionExecutor` extension command path.** This is how KS-HD-* models expose vendor extensions on top of FTMS (programs, calibration). Methods include `extVoidCmd`, `extParamCmd`, `getFuncList`, `voidActionById`, `_parseVoidFunRes`. The exact opcodes are encoded as `funId`s and live in the `ProtocolDeviceAction` subclass tables.

5. **Per-model capability flags.** `DeviceConfig` (in `package:base/model/products.dart`) is a giant capability map with getters like `isWilink`, `supportAutoStop`, `supportGradient`, `supportHeartRate`, `canSwitchAutoModeRunning`, `minWalkingMaxIs2`, etc. The `WalkingPadController` currently has no per-model capability flags exposed to callers — adding them would let HA show/hide UI based on what each model can actually do.

## Tooling / repro

```bash
# Extract Flutter native libs from APK
unzip KS_Fit-5.9.10.apk "lib/arm64-v8a/lib*.so"

# Build blutter (one-time)
git clone https://github.com/worawit/blutter
cd blutter && python3 blutter.py /path/to/lib/arm64-v8a /path/to/output

# blutter produces:
#   asm/<package>/...     — disassembled Dart bytecode per source file
#   pp.txt                — Dart object pool (string constants, type args, anon closures)
#   objs.txt              — class/method index
#   blutter_frida.js      — Frida script for runtime instrumentation
#   ida_script/           — IDA Pro / Ghidra import scripts

# Useful starting points
grep -rln "d18d2c10" asm/                         # → ks_blue/src/wilink/wilink_device.dart
grep -rln "WilinkDeviceOdmExt" asm/
grep -rln "ODMSupplement\|FTMSSupplement" asm/
grep -rln "isMC21" asm/                            # → wilink_device.dart line 17275
grep -nE "^  [a-zA-Z_].*\(" asm/.../<file>.dart    # method index per file
```

If we want to validate the model-dispatch logic at runtime, blutter's Frida script can hook every Dart method and dump arguments — pointing it at a live KS Fit + MC-21 session would resolve the `field_43` question in minutes.
