"""Bisect which connection-setup step causes the device to disconnect.

Each step is performed on a fresh connection.  After the step we sit
idle for IDLE_OBSERVATION seconds and record when (if) the device
disconnects.

Steps, additive:
  S0: BleakClient connect only (vanilla)
  S1: + service discovery  (services already discovered by Bleak on connect)
  S2: + read FTMS Feature (0x2acc)
  S3: + read Supported Speed Range (0x2ad4)
  S4: + subscribe Treadmill Data (0x2acd)
  S5: + subscribe Fitness Machine Status (0x2ada)
  S6: + subscribe Control Point indications (0x2ad9)
  S7: + write REQUEST_CONTROL (0x00 to 0x2ad9)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from bleak import BleakClient, BleakScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("bleak").setLevel(logging.WARNING)
_LOGGER = logging.getLogger("step")

DEVICE_NAME = "KS-HD-Z1D"
IDLE_OBSERVATION = 20.0
INTER_STEP_GAP = 8.0  # seconds between scenarios

FTMS_FEATURE = "00002acc-0000-1000-8000-00805f9b34fb"
SUPPORTED_SPEED_RANGE = "00002ad4-0000-1000-8000-00805f9b34fb"
TREADMILL_DATA = "00002acd-0000-1000-8000-00805f9b34fb"
MACHINE_STATUS = "00002ada-0000-1000-8000-00805f9b34fb"
CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"


async def find_device():
    d = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if d is None:
        _LOGGER.error("device not found")
        sys.exit(1)
    return d


async def probe_step(name: str, do_after_connect):
    """Connect, run do_after_connect(client), then idle and time disconnect."""
    last_err = None
    for attempt in range(1, 6):
        try:
            device = await find_device()
            disc_event = asyncio.Event()
            disc_at = [0.0]

            def on_disc(_):
                disc_at[0] = time.time()
                disc_event.set()

            client = BleakClient(device, disconnected_callback=on_disc)
            await client.connect()
            break
        except Exception as err:
            last_err = err
            _LOGGER.warning("[%s] connect attempt %d failed: %s", name, attempt, err)
            await asyncio.sleep(4)
    else:
        _LOGGER.error("[%s] all connect attempts failed: %s", name, last_err)
        return f"connect-failed: {last_err}"
    try:
        await do_after_connect(client)
    except Exception as err:
        _LOGGER.warning("[%s] setup raised: %s", name, err)
    last_action = time.time()
    _LOGGER.info("[%s] setup complete, idling…", name)

    deadline = last_action + IDLE_OBSERVATION
    while time.time() < deadline and not disc_event.is_set():
        await asyncio.sleep(0.2)
    if disc_event.is_set():
        elapsed = disc_at[0] - last_action
        _LOGGER.warning("[%s] DISCONNECTED after %.2fs idle", name, elapsed)
        verdict = f"disc@{elapsed:.1f}s"
    else:
        _LOGGER.info("[%s] survived %.0fs", name, IDLE_OBSERVATION)
        verdict = f"survived {IDLE_OBSERVATION:.0f}s"

    try:
        if client.is_connected:
            await client.disconnect()
    except Exception:
        pass
    return verdict


async def main():
    cp_response = bytearray()

    async def s0_noop(client):
        pass

    async def s1_discover(client):
        # Already discovered by .connect(); just walk the tree.
        for svc in client.services:
            for ch in svc.characteristics:
                pass

    async def s2_read_feature(client):
        await s1_discover(client)
        v = await client.read_gatt_char(FTMS_FEATURE)
        _LOGGER.info("  read FTMS_FEATURE = %s", v.hex())

    async def s3_read_speedrange(client):
        await s2_read_feature(client)
        v = await client.read_gatt_char(SUPPORTED_SPEED_RANGE)
        _LOGGER.info("  read SUPPORTED_SPEED_RANGE = %s", v.hex())

    async def s4_sub_treadmill(client):
        await s3_read_speedrange(client)
        await client.start_notify(TREADMILL_DATA, lambda *_: None)

    async def s5_sub_status(client):
        await s4_sub_treadmill(client)
        await client.start_notify(MACHINE_STATUS, lambda *_: None)

    async def s6_sub_cp(client):
        await s5_sub_status(client)
        await client.start_notify(CONTROL_POINT, lambda *_: None)

    async def s7_request_control(client):
        await s6_sub_cp(client)
        await client.write_gatt_char(CONTROL_POINT, b"\x00", response=True)

    steps = [
        ("S0 connect-only", s0_noop),
        ("S1 +discover", s1_discover),
        ("S2 +read feature", s2_read_feature),
        ("S3 +read speedrange", s3_read_speedrange),
        ("S4 +sub treadmill", s4_sub_treadmill),
        ("S5 +sub status", s5_sub_status),
        ("S6 +sub control-pt", s6_sub_cp),
        ("S7 +REQUEST_CONTROL", s7_request_control),
    ]

    results = []
    for name, fn in steps:
        _LOGGER.info("=== %s ===", name)
        v = await probe_step(name, fn)
        results.append((name, v))
        await asyncio.sleep(INTER_STEP_GAP)

    _LOGGER.info("=" * 60)
    _LOGGER.info("RESULTS")
    _LOGGER.info("=" * 60)
    for name, v in results:
        _LOGGER.info("  %-25s %s", name, v)


if __name__ == "__main__":
    asyncio.run(main())
