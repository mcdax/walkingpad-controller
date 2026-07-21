"""Decisive test: does adding traffic prevent disconnects?

Three parallel scenarios, each with a fresh connection:

A: silent  — connect, then sit idle. No GATT traffic from us.
B: read-1Hz — connect, then read Battery Level once per second.
C: read-fast — connect, then read Battery Level every 200 ms.
D: write-fast — connect, then re-write CCCD on Treadmill Data every
                 second (lots of writes from us).

For each: log every notification we receive, log every disconnect with
its timestamp, log every read latency. The hypothesis "lib not sending
enough traffic causes disconnects" predicts: A disconnects fastest, D
holds longest. The hypothesis "signal/firmware" predicts: roughly the
same.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from bleak import BleakClient, BleakScanner

from commands._common import DEFAULT_DEVICE_NAME, setup_logging  # noqa: E402

_LOGGER = logging.getLogger("keepalive")

BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
TREADMILL_DATA_UUID = "00002acd-0000-1000-8000-00805f9b34fb"
TM_CCCD_HANDLE = None  # discovered at runtime


@dataclass
class ScenarioResult:
    name: str
    description: str
    connected_at: float = 0.0
    disconnected_at: float = 0.0
    held_seconds: float = 0.0
    activity_count: int = 0
    last_activity_ok: bool = True
    notif_count: int = 0
    read_latencies_ms: list[float] = field(default_factory=list)
    early_failure: str | None = None


async def find_device(name: str):
    return await BleakScanner.find_device_by_name(name, timeout=15)


async def run_scenario(
    name: str,
    description: str,
    activity_fn,
    duration: float,
    device_name: str,
) -> ScenarioResult:
    res = ScenarioResult(name=name, description=description)
    device = await find_device(device_name)
    if device is None:
        res.early_failure = "device-not-found"
        return res

    disc_event = asyncio.Event()

    def on_disc(_):
        res.disconnected_at = time.time()
        disc_event.set()

    client = BleakClient(device, disconnected_callback=on_disc)
    try:
        await client.connect()
    except Exception as err:
        res.early_failure = f"connect-failed: {err}"
        return res

    res.connected_at = time.time()
    if not client.is_connected:
        res.early_failure = "not-connected-post-connect"
        return res

    # Subscribe to Treadmill Data to count incoming notifications (a bystander
    # signal — both scenarios receive these the same way, so notifs only tell
    # us whether the link is up, not whether our traffic helped)
    def on_notif(_h, _d):
        res.notif_count += 1

    with contextlib.suppress(Exception):
        await client.start_notify(TREADMILL_DATA_UUID, on_notif)

    deadline = time.time() + duration
    last_log = res.connected_at
    while time.time() < deadline and not disc_event.is_set():
        if activity_fn is not None:
            t0 = time.time()
            try:
                await activity_fn(client)
                res.activity_count += 1
                res.last_activity_ok = True
                res.read_latencies_ms.append((time.time() - t0) * 1000.0)
            except Exception as err:
                res.last_activity_ok = False
                # don't break — keep counting, but record
                res.early_failure = f"activity-failed at {time.time() - res.connected_at:.1f}s: {err}"
                break
        # Heartbeat
        now = time.time()
        if now - last_log > 5:
            elapsed = now - res.connected_at
            _LOGGER.info(
                "  %s: alive %.0fs, activity=%d, notifs=%d, conn=%s",
                name, elapsed, res.activity_count, res.notif_count, client.is_connected,
            )
            last_log = now

    # Compute held time
    if disc_event.is_set():
        res.held_seconds = res.disconnected_at - res.connected_at
    else:
        res.held_seconds = duration

    # Cleanup
    with contextlib.suppress(Exception):
        if client.is_connected:
            await client.disconnect()
    return res


# Activity functions

async def silent_activity(_client):
    # Do nothing — but yield to event loop
    await asyncio.sleep(0.5)


async def read_1hz(client):
    await client.read_gatt_char(BATTERY_LEVEL_UUID)
    await asyncio.sleep(1.0)


async def read_fast(client):
    await client.read_gatt_char(BATTERY_LEVEL_UUID)
    await asyncio.sleep(0.2)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--duration", type=float, default=45.0,
                   help="seconds to observe each scenario")
    p.add_argument("--inter-gap", type=float, default=8.0,
                   help="seconds between scenarios")
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--log", default="INFO")
    args = p.parse_args()
    setup_logging(args.log)

    scenarios = [
        ("A_silent",   "no GATT activity",            silent_activity),
        ("B_read_1Hz", "Battery Level read 1× / sec", read_1hz),
        ("C_read_5Hz", "Battery Level read 5× / sec", read_fast),
    ]

    all_results: list[ScenarioResult] = []
    for rep in range(1, args.reps + 1):
        for sname, sdesc, sfn in scenarios:
            label = f"{sname}#{rep}"
            print(f"\n=== rep {rep}: {label} — {sdesc} ===")
            r = await run_scenario(label, sdesc, sfn, args.duration, args.name)
            all_results.append(r)
            verdict = "  -> "
            if r.early_failure:
                verdict += f"EARLY FAIL ({r.early_failure}) at held {r.held_seconds:.1f}s"
            else:
                outcome = "still connected" if r.disconnected_at == 0 else f"disconnected after {r.held_seconds:.1f}s"
                avg_lat = (sum(r.read_latencies_ms) / len(r.read_latencies_ms)
                           if r.read_latencies_ms else 0)
                verdict += f"{outcome} | activity={r.activity_count} avg-lat={avg_lat:.0f}ms | notifs={r.notif_count}"
            print(verdict)
            await asyncio.sleep(args.inter_gap)

    # Summary
    print()
    print("=" * 80)
    print("SUMMARY (by scenario, held seconds)")
    print("=" * 80)
    by_name: dict[str, list[float]] = {}
    for r in all_results:
        bare = r.name.split("#")[0]
        by_name.setdefault(bare, []).append(r.held_seconds)

    print(f"  {'scenario':<14} {'reps':>4} {'min':>7} {'avg':>7} {'max':>7}  description")
    for sname, sdesc, _ in scenarios:
        held = by_name.get(sname, [])
        if not held:
            continue
        mn = min(held); mx = max(held); avg = sum(held) / len(held)
        print(f"  {sname:<14} {len(held):>4} {mn:>7.1f} {avg:>7.1f} {mx:>7.1f}  {sdesc}")

    print()
    print("Interpretation:")
    print("  - If A held >> B/C: signal/firmware-driven disconnects, traffic doesn't help")
    print("  - If C held >> A: more traffic helps, suggests keepalive theory")
    print("  - If they're all roughly equal: traffic isn't the lever")


if __name__ == "__main__":
    asyncio.run(main())
