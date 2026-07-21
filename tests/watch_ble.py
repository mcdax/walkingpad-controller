"""Passive BLE watcher.

Connects to the device, walks every characteristic in the GATT tree, and
subscribes to anything that supports notify or indicate.  Every received
frame is timestamped, decoded if possible, and printed.

Run alongside the per-command scripts (in another terminal) to correlate
which BLE traffic each command produces.

Outputs to stdout.  --csv writes a tab-separated event log instead.

Note: this is *passive* on the GATT layer — we can't see what the central
*sends* without root-level btmon.  It captures what the device emits.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from bleak import BleakClient, BleakScanner

from commands._common import DEFAULT_DEVICE_NAME, setup_logging  # noqa: E402

# Friendly names for chars we know.
KNOWN: dict[str, str] = {
    "00002a05-0000-1000-8000-00805f9b34fb": "Service Changed",
    "00002a19-0000-1000-8000-00805f9b34fb": "Battery Level",
    "00002acc-0000-1000-8000-00805f9b34fb": "FTMS Feature",
    "00002acd-0000-1000-8000-00805f9b34fb": "Treadmill Data",
    "00002ad3-0000-1000-8000-00805f9b34fb": "Training Status",
    "00002ad4-0000-1000-8000-00805f9b34fb": "Supported Speed Range",
    "00002ad5-0000-1000-8000-00805f9b34fb": "Supported Inclination Range",
    "00002ad9-0000-1000-8000-00805f9b34fb": "FTMS Control Point",
    "00002ada-0000-1000-8000-00805f9b34fb": "Fitness Machine Status",
    "24e2521c-f63b-48ed-85be-c5330b00fdf7": "Supplement Notify",
    "0000fff1-0000-1000-8000-00805f9b34fb": "Vendor 0xFFF1 Notify",
    "0000ffc1-0000-1000-8000-00805f9b34fb": "Vendor 0xFFC1 Notify",
}


def short(uuid: str) -> str:
    """Render a 128-bit UUID compactly when it's a 16-bit alias."""
    if uuid.startswith("0000") and uuid.endswith("-0000-1000-8000-00805f9b34fb"):
        return f"0x{uuid[4:8].upper()}"
    return uuid[:8] + "…"


def decode_treadmill_data(data: bytes) -> str:
    if len(data) < 4:
        return "(short)"
    flags = struct.unpack_from("<H", data, 0)[0]
    speed = struct.unpack_from("<H", data, 2)[0] / 100.0
    parts = [f"flags=0x{flags:04x}", f"speed={speed:.2f}"]
    off = 4
    if flags & 0x0004 and off + 3 <= len(data):
        d = data[off] | (data[off + 1] << 8) | (data[off + 2] << 16)
        parts.append(f"dist={d}m")
        off += 3
    if flags & 0x0080 and off + 5 <= len(data):
        cal = struct.unpack_from("<H", data, off)[0]
        parts.append(f"cal={cal}")
        off += 5
    if flags & 0x0400 and off + 2 <= len(data):
        t = struct.unpack_from("<H", data, off)[0]
        parts.append(f"time={t}s")
        off += 2
    if flags & 0x2000 and off + 3 <= len(data):
        steps = struct.unpack_from("<H", data, off)[0]
        parts.append(f"steps={steps}")
        off += 3
    return " ".join(parts)


def decode_fm_status(data: bytes) -> str:
    if not data:
        return "(empty)"
    op = data[0]
    NAME = {
        0x01: "RESET", 0x02: "STOPPED_OR_PAUSED", 0x03: "STOPPED_BY_SAFETY",
        0x04: "STARTED", 0x05: "TARGET_SPEED", 0x06: "TARGET_INCLINE",
        0x14: "SPIN_DOWN", 0xFF: "CONTROL_LOST",
    }
    name = NAME.get(op, f"0x{op:02x}")
    if op == 0x05 and len(data) >= 3:
        v = struct.unpack_from("<H", data, 1)[0] / 100.0
        return f"{name} target={v:.2f} km/h"
    if op == 0x02 and len(data) >= 2:
        return f"{name} param={data[1]:#x}"
    return name


def decode_cp_response(data: bytes) -> str:
    if len(data) < 3 or data[0] != 0x80:
        return data.hex()
    return f"resp opcode=0x{data[1]:02x} result=0x{data[2]:02x}"


def decode_training_status(data: bytes) -> str:
    if len(data) < 2:
        return data.hex()
    NAMES = {
        0: "OTHER", 1: "IDLE", 2: "WARMING_UP",
        3: "LOW_INT", 4: "HIGH_INT", 5: "RECOVERY",
        12: "MANUAL", 13: "PRE_WORKOUT", 14: "POST_WORKOUT",
    }
    flags, status = data[0], data[1]
    return f"flags=0x{flags:02x} status={NAMES.get(status, hex(status))}"


_DECODERS = {
    "00002acd-0000-1000-8000-00805f9b34fb": decode_treadmill_data,
    "00002ada-0000-1000-8000-00805f9b34fb": decode_fm_status,
    "00002ad9-0000-1000-8000-00805f9b34fb": decode_cp_response,
    "00002ad3-0000-1000-8000-00805f9b34fb": decode_training_status,
}


async def main():
    p = argparse.ArgumentParser(description="Passive BLE watcher for KingSmith FTMS")
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--csv", type=Path, default=None,
                   help="if set, write tab-separated log here instead of stdout")
    p.add_argument("--duration", type=float, default=300.0,
                   help="seconds to watch before exiting")
    p.add_argument("--log", default="WARNING")
    p.add_argument("--include-non-notifiable", action="store_true",
                   help="also log readable characteristics by polling once at start")
    args = p.parse_args()
    setup_logging(args.log)

    device = await BleakScanner.find_device_by_name(args.name, timeout=15)
    if device is None:
        print(f"device {args.name!r} not found", file=sys.stderr)
        return 1

    csv_handle = open(args.csv, "w") if args.csv else None
    if csv_handle:
        csv_handle.write("ts\thandle\tuuid\tname\tdir\tlen\thex\tdecoded\n")

    t0 = time.time()

    def emit(direction: str, char_uuid: str, data: bytes):
        ts = time.time() - t0
        name = KNOWN.get(char_uuid, "?")
        decoded = ""
        if char_uuid in _DECODERS:
            with contextlib.suppress(Exception):
                decoded = _DECODERS[char_uuid](bytes(data))
        if csv_handle:
            csv_handle.write(
                f"{ts:.3f}\t-\t{char_uuid}\t{name}\t{direction}\t{len(data)}\t{bytes(data).hex()}\t{decoded}\n"
            )
            csv_handle.flush()
        else:
            print(
                f"[{ts:8.3f}] {direction:3s} {short(char_uuid):11s} {name:24s} "
                f"len={len(data):3d}  {bytes(data).hex():32s}  {decoded}"
            )

    print(f"connecting to {device.address}…")
    async with BleakClient(device) as client:
        if not client.is_connected:
            print("connect failed", file=sys.stderr)
            return 1
        print("connected.  walking GATT tree…")

        # Snapshot service tree
        for svc in client.services:
            print(f"  service {short(svc.uuid):11s} {svc.uuid}")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                tag = KNOWN.get(ch.uuid.lower(), "")
                print(f"    char {ch.handle:3d}  {short(ch.uuid):11s} {ch.uuid}  ({props}) {tag}")

        # Subscribe to everything notifiable
        sub_count = 0
        for svc in client.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await client.start_notify(
                            ch.uuid,
                            lambda _h, d, u=ch.uuid: emit("rx", u, d),
                        )
                        sub_count += 1
                    except Exception as err:
                        print(f"    !subscribe {ch.uuid}: {err}")

        # Optional: snapshot readable characteristics
        if args.include_non_notifiable:
            for svc in client.services:
                for ch in svc.characteristics:
                    if "read" in ch.properties:
                        try:
                            v = await client.read_gatt_char(ch.uuid)
                            emit("rd", ch.uuid, v)
                        except Exception as err:
                            print(f"    !read {ch.uuid}: {err}")

        print(f"subscribed to {sub_count} characteristics; observing for {args.duration:.0f}s…")
        try:
            await asyncio.sleep(args.duration)
        except KeyboardInterrupt:
            pass

    if csv_handle:
        csv_handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
