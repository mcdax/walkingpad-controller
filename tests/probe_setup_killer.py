"""Bisect which connection-setup step destabilises the link.

Earlier finding: bare BleakClient.connect() + idle 45 s holds reliably.
Lib's full setup (subscribe ×4 + reads + REQUEST_CONTROL) disconnects in
~3 s. So one (or several) of those ops is the trigger.

Each scenario starts from a fresh connect, performs a specific subset of
the setup, then idles for IDLE seconds. We record the actual time
to disconnect.

Scenarios (additive):
  S0: connect-only
  S1: + subscribe Treadmill Data (notify)
  S2: + subscribe FM Status (notify)
  S3: + subscribe Training Status (notify)
  S4: + subscribe Control Point (INDICATE — different from notify, suspicious)
  S5: + read FTMS Feature
  S6: + read Supported Speed Range
  S7: + write REQUEST_CONTROL
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from bleak import BleakClient, BleakScanner

from commands._common import DEFAULT_DEVICE_NAME, setup_logging  # noqa: E402

_LOGGER = logging.getLogger("killer")

FTMS_FEATURE = "00002acc-0000-1000-8000-00805f9b34fb"
SUPPORTED_SPEED_RANGE = "00002ad4-0000-1000-8000-00805f9b34fb"
TREADMILL_DATA = "00002acd-0000-1000-8000-00805f9b34fb"
MACHINE_STATUS = "00002ada-0000-1000-8000-00805f9b34fb"
TRAINING_STATUS = "00002ad3-0000-1000-8000-00805f9b34fb"
CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"


@dataclass
class Result:
    name: str
    setup_ok: bool
    held_seconds: float
    setup_error: str | None = None
    disconnect_during: str | None = None  # "setup" or "idle"


async def find_device(name: str):
    return await BleakScanner.find_device_by_name(name, timeout=15)


async def run_scenario(label: str, setup_fn, idle: float, device_name: str) -> Result:
    res = Result(name=label, setup_ok=False, held_seconds=0.0)
    device = await find_device(device_name)
    if device is None:
        res.setup_error = "device-not-found"
        return res

    disc_event = asyncio.Event()
    disc_at = [0.0]

    def on_disc(_):
        disc_at[0] = time.time()
        disc_event.set()

    client = BleakClient(device, disconnected_callback=on_disc)
    try:
        await client.connect()
    except Exception as err:
        res.setup_error = f"connect: {err}"
        return res
    if not client.is_connected:
        res.setup_error = "not-connected-post-connect"
        return res
    connected_at = time.time()

    # Run the setup operations
    try:
        await setup_fn(client)
        res.setup_ok = True
    except Exception as err:
        res.setup_error = f"setup: {err}"
        if disc_event.is_set():
            res.disconnect_during = "setup"
            res.held_seconds = disc_at[0] - connected_at
            return res
        # else fall through and idle anyway

    # Idle and watch
    deadline = time.time() + idle
    while time.time() < deadline and not disc_event.is_set():
        await asyncio.sleep(0.2)
    if disc_event.is_set():
        res.disconnect_during = "idle" if res.setup_ok else "setup"
        res.held_seconds = disc_at[0] - connected_at
    else:
        res.held_seconds = idle  # survived
        res.disconnect_during = None

    with contextlib.suppress(Exception):
        if client.is_connected:
            await client.disconnect()
    return res


# Setup functions, additive

async def setup_connect_only(c):
    pass


async def setup_sub_treadmill(c):
    await c.start_notify(TREADMILL_DATA, lambda *_: None)


async def setup_sub_fm(c):
    await setup_sub_treadmill(c)
    await c.start_notify(MACHINE_STATUS, lambda *_: None)


async def setup_sub_training(c):
    await setup_sub_fm(c)
    await c.start_notify(TRAINING_STATUS, lambda *_: None)


async def setup_sub_cp(c):
    await setup_sub_training(c)
    await c.start_notify(CONTROL_POINT, lambda *_: None)


async def setup_read_feature(c):
    await setup_sub_cp(c)
    await c.read_gatt_char(FTMS_FEATURE)


async def setup_read_speedrange(c):
    await setup_read_feature(c)
    await c.read_gatt_char(SUPPORTED_SPEED_RANGE)


async def setup_request_control(c):
    await setup_read_speedrange(c)
    await c.write_gatt_char(CONTROL_POINT, b"\x00", response=True)


SCENARIOS = [
    ("S0_connect_only",   setup_connect_only),
    ("S1_sub_treadmill",  setup_sub_treadmill),
    ("S2_sub_fm",         setup_sub_fm),
    ("S3_sub_training",   setup_sub_training),
    ("S4_sub_cp_indic",   setup_sub_cp),
    ("S5_read_feature",   setup_read_feature),
    ("S6_read_speedrange", setup_read_speedrange),
    ("S7_request_control", setup_request_control),
]


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--idle", type=float, default=30.0)
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--log", default="WARNING")
    args = p.parse_args()
    setup_logging(args.log)

    all_results: list[Result] = []
    for rep in range(1, args.reps + 1):
        for label, setup_fn in SCENARIOS:
            print(f"\n=== rep {rep}: {label} ===")
            r = await run_scenario(f"{label}#{rep}", setup_fn, args.idle, args.name)
            all_results.append(r)
            verdict = (
                f"setup_ok={r.setup_ok} held={r.held_seconds:5.1f}s "
                f"disc_during={r.disconnect_during} err={r.setup_error}"
            )
            print(f"  -> {verdict}")
            await asyncio.sleep(6)

    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    by_name: dict[str, list[Result]] = {}
    for r in all_results:
        bare = r.name.split("#")[0]
        by_name.setdefault(bare, []).append(r)

    print(f"  {'scenario':<22} {'reps':>4} {'min_held':>9} {'avg_held':>9} {'survivors':>10}")
    for label, _ in SCENARIOS:
        rs = by_name.get(label, [])
        if not rs:
            continue
        held = [r.held_seconds for r in rs]
        survivors = sum(1 for r in rs if r.disconnect_during is None)
        print(f"  {label:<22} {len(rs):>4} {min(held):>9.1f} {sum(held)/len(held):>9.1f} {survivors:>10}/{len(rs)}")


if __name__ == "__main__":
    asyncio.run(main())
