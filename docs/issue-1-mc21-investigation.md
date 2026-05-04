# Issue #1 — KingSmith MC-21 speed control investigation

Background: https://github.com/mcdax/walkingpad-controller/issues/1

User report (`@flyzet-prog`): on a `KS-MC21-D06BFD` treadmill, start/stop work via the HA integration, but the speed slider has no effect. The HA slider only ever reflects what the physical remote sets. Reverse engineering with `nRF Connect` showed BLE writes are rejected unless KS Fit is opened first.

## TL;DR

Speed control over BLE is **not possible on the MC-21**. KS Fit cannot do it either — the `setSpeed` function in KS Fit's BLE module exists only in the legacy WiLink (`0xFE00`) protocol path, and the MC-21 doesn't expose that service. Start/stop is the only thing that works on the MC-21 over BLE.

The library can still be improved: it currently aborts when FTMS `REQUEST_CONTROL` fails, but KS Fit ignores that failure and proceeds. Mirroring that behavior would make start/stop reliable on MC-21.

## Evidence

### From the HCI snoop logs

User attached three Bluetooth HCI snoop logs (~17 MB total). Decoded with `btmon -r`.

GATT services advertised by the MC-21:

| Service | UUID | Notes |
|---|---|---|
| Generic Access Profile | `0x1800` | standard |
| Generic Attribute Profile | `0x1801` | standard |
| Fitness Machine | `0x1826` | FTMS — handles `0x000e–0x0028` |
| Device Information | `0x180a` | standard |
| Battery Service | `0x180f` | standard |
| TI vendor service | `f000ffc0-0451-4000-b000-000000000000` | TI BLE-Stack default base UUID; KS Fit does not reference it |

Notable absences:

- **No** `0x0000FE00` (legacy WiLink service) — this is what KingSmith's older treadmills (and the `ph4-walkingpad` library) use.
- **No** `24e2521c-…fdf7` (KS-HD-* "supplement" service) — used on other FTMS-based KingSmith models for vendor commands.

Inside the FTMS service there is one extra writable characteristic:

- Handle `0x0028`, UUID `d18d2c10-c44c-11e8-a355-529269fb1459`, properties `0x08` (write only).

KS Fit's behavior in the snoop:

1. Writes a fixed 8-byte payload `01 00 0d 00 06 0b 0f 0d` to handle `0x0028`. Done 40 times across the session, byte-for-byte identical, so the payload is *not* dynamic data (not speed, not a timestamp).
2. Writes `00` (`REQUEST_CONTROL`) to the FTMS Control Point (`0x0022`). The device responds **every single time** with the indication `80 00 04` — i.e. response code `0x80`, request opcode `0x00`, result `0x04` (`OPERATION_FAILED`). 40 attempts, 40 failures.
3. Despite that, KS Fit writes `07` (`START_OR_RESUME`) anyway, and the belt actually starts moving (Treadmill Data later shows speed climbing to 2.0 km/h).
4. **In the entire 16 MB log, KS Fit never writes opcode `0x02` (`SET_TARGET_SPEED`).** Only `0x00` and `0x07`.
5. Pairing/bonding is attempted by the Android stack (SMP `Pairing Request`) and fails (`Pairing Failed (0x05)` with reason "Unspecified"). The treadmill operates without bonding.

### From the KS Fit APKs

User attached two APKs:

- `KS_Fit-5.9.10.apk` (133 MB) — older release. arm64-v8a libs included.
- `KS Fit_6.0.7_APKPure.xapk` (185 MB) — newer release in xapk format. Only `config.armeabi_v7a.apk` split bundled.

KS Fit is built on Flutter, so the `.dex` files are mostly Flutter framework boilerplate; the actual application logic is in `lib/<arch>/libapp.so` as a Dart AOT snapshot. No Dart decompiler was used here — only `strings` on the AOT binary, which exposes class names, method names, source file paths, and log format strings via the symbol table.

Key strings found in `libapp.so` (cross-checked between v5.9.10 arm64 and v6.0.7 armv7):

```
package:ks_blue/src/wilink/wilink_protocol.dart
package:ks_blue/src/wilink/wilink_device.dart
package:ks_blue/src/wilink/treadmill_data.dart
package:ks_blue/src/wilink/ftms_action_executor.dart
package:ks_blue/src/spinning/spinning_ftms_device.dart
package:ks_blue/src/ble/protocol.dart

WilinkDeviceActionExt|setSpeed
WilinkDeviceActionExt|_setSpeedCommand
------do setSpeed new speed:
-----> WilinkBleDevice _winlinkDevice.setSpeed :
setSpeed new:

KsTreadmillDevice
KsTreadmillDevice startOrPause mode:
KsTreadmillDevice listenRunningStateChange event:
Init KsTreadmillDevice bleModel:

FtmsActionExecutor
FtmsActionExecutor extParamCmd
FtmsActionExecutor extVoidCmd
FtmsActionExecutor supplement STR action[
FtmsActionExecutor supplement UINT16 action[
FtmsActionExecutor supplement Void action[

2ad9: request control, result:
2ad9: request start, result:
2ad9: request pause or stop, result:
2ad9: request reset, result:
2ad9: response:
2ad9: write error:

HW-KS-HC-MC21A
KS-MC21
KS-SMC21C
get:isMC21
get:plan_not_support_mc21
_uploadMc21Record@1418084129
--> mc21 fireRecord
```

Interpretation:

- **`setSpeed` lives only in the WiLink path.** The only string `setSpeed` references (across both APK versions) are `WilinkDeviceActionExt|setSpeed`, `WilinkDeviceActionExt|_setSpeedCommand`, `WilinkBleDevice _winlinkDevice.setSpeed`. There is no FTMS-side `setSpeed`. The WiLink protocol requires the `0xFE00` service, which the MC-21 doesn't have.
- **`KsTreadmillDevice` is the FTMS treadmill class.** Its visible methods are `startOrPause`, `listenRunningStateChange`, `close`, `Init`. No setSpeed, no setTargetSpeed.
- **`FtmsActionExecutor` is the FTMS extension command path.** It works against the supplement service (`24e2521c-…`), with `extParamCmd` / `extVoidCmd` / `supplement STR/UINT16/Void action`. The MC-21 doesn't have the supplement service, so this path also doesn't apply.
- **KS Fit explicitly knows the MC-21 is constrained.** `isMC21` getter, `plan_not_support_mc21` flag, separate `_uploadMc21Record` upload routine, dedicated MC-21 logging. The hardware identifier `HW-KS-HC-MC21A` confirms model recognition.

The vendor write to handle `0x0028` (`01 00 0d 00 06 0b 0f 0d`) does not appear to come from any documented control flow in the Dart code — based on the strings, it's almost certainly KS Fit's BLE plugin opportunistically writing to a discovered writable characteristic, not a deliberate handshake. The snoops also show it doesn't unlock anything: `REQUEST_CONTROL` still fails after the write.

### Why the user's HA log shows result `5` while KS Fit gets result `4`

User's HA log: `FTMS: Command 0x00 result: 5` (`CONTROL_NOT_PERMITTED`).
KS Fit snoop: response indication `80 00 04` (`OPERATION_FAILED`).

Both mean "no, can't do that." The exact code probably depends on prior state (whether some other client recently held control, whether the device just powered on, etc.). It's not the load-bearing detail — the load-bearing detail is that `REQUEST_CONTROL` fails on this device regardless of caller, and KS Fit just ignores it.

## Implications for the library

1. **Add MC-21 to FTMS name detection.** `FTMS_NAME_PREFIXES` is currently `("KS-HD-",)`. The MC-21 advertises as `KS-MC21-…` and falls through to the service-UUID probe path — that part works correctly today, but adding `"KS-MC21-"` (and likely `"KS-SMC21"` per the strings) would skip the probe round-trip on first connect.
2. **Tolerate `REQUEST_CONTROL` rejection.** `FTMSController._request_control` sets `_has_control` only on success; subsequent commands then keep retrying `REQUEST_CONTROL`. Per the snoop, the MC-21 will *never* grant control via REQUEST_CONTROL, but it does accept `START_OR_RESUME` / `STOP_OR_PAUSE` directly. The library should attempt control once, log the failure, and proceed — same as KS Fit. Don't gate start/stop on `_has_control`.
3. **Document MC-21 speed limitation.** Add a note (README "Known Behavior" or a per-device caveat) that on MC-21 the BLE interface exposes start/stop and telemetry only; speed is controlled exclusively by the physical remote. The slider in HA will reflect the physical speed but cannot drive it. This is a firmware constraint, not a library bug.
4. **Optional: emit a friendlier error if `SET_TARGET_SPEED` is rejected.** Today it just logs `result: 5` warnings. On a known-constrained model like MC-21 we could short-circuit and either no-op the call or raise a clearer "speed control not supported on this device" exception.

## What's not yet confirmed

The strings analysis is suggestive but not definitive — class and method names exist in the symbol table, but their actual call graphs do not. To rule out the possibility that KS Fit drives speed via the vendor characteristic at handle `0x0028` with a payload format the snoop didn't capture, the user would need to:

1. Start a fresh HCI snoop.
2. Open KS Fit, connect, start the belt.
3. Explicitly drag the speed slider in the app and confirm the belt physically responds (without touching the remote).
4. Attach the resulting log.

If that capture also contains only the fixed `01 00 0d 00 06 0b 0f 0d` writes, "no BLE speed control on MC-21" is settled. If it contains a different payload, that's the missing command path and worth implementing.

A proper Dart decompiler (`blutter`, `reFlutter`, `Doldrums`) would also resolve the question by direct inspection of the Dart code — not used here.

## Methodology / repro

```bash
# Decode HCI snoop logs
btmon -r btsnoop_hci_*.log > decoded.txt

# Find FTMS Control Point activity
grep -nE "Handle: 0x0022 Type: Fitness Machine Control Point|Data\[" decoded.txt

# Extract Flutter AOT binary from APK
unzip APK.apk "lib/<arch>/libapp.so"

# String inventory
strings -a libapp.so | grep -iE "setSpeed|FTMS|wilink|MC21|2ad9"
```
