"""Focused stress test for the 'can't restart after stop' failure mode.

Run patterns: many start/stop cycles with varying gap lengths between
the stop and the next start, plus a "rapid burst" pattern.  Logs every
control-point opcode, every result, every disconnect, every status
update — so when something fails we can correlate.

If a start fails after a stop, the test attempts diagnostic recovery
to surface root cause: re-request control, reconnect, retry.
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from bleak import BleakScanner

from walkingpad_controller import ProtocolType, TreadmillStatus, WalkingPadController

# Verbose logging on the library so we see every CP write + result.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet bleak's chatter
logging.getLogger("bleak").setLevel(logging.INFO)
logging.getLogger("bleak.backends").setLevel(logging.INFO)
_LOGGER = logging.getLogger("stress")

DEVICE_NAME = "KS-HD-Z1D"

GAP_PATTERNS = [
    ("rapid", 0.5),
    ("short", 2.0),
    ("medium", 5.0),
    ("long", 10.0),
]
CYCLES_PER_PATTERN = 3
RUN_DURATION = 4.0  # seconds belt runs at speed before stop


@dataclass
class CycleResult:
    cycle: int
    pattern: str
    gap_before_start: float
    start_ok: bool = False
    set_speed_ok: bool = False
    stop_ok: bool = False
    disconnected: bool = False
    notes: list[str] = field(default_factory=list)


async def find_device():
    _LOGGER.info("Scanning for %s...", DEVICE_NAME)
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if device is None:
        _LOGGER.error("Device %s not found.", DEVICE_NAME)
        sys.exit(1)
    _LOGGER.info("Found: %s (%s)", device.name, device.address)
    return device


class TestRig:
    def __init__(self, device):
        self.device = device
        self.controller: WalkingPadController | None = None
        self.disconnected = asyncio.Event()
        self.status_count = 0
        self.last_status: TreadmillStatus | None = None
        self.results: list[CycleResult] = []

    def on_status(self, status: TreadmillStatus) -> None:
        self.status_count += 1
        self.last_status = status

    def on_disconnect(self) -> None:
        _LOGGER.warning("DISCONNECT callback fired")
        self.disconnected.set()

    async def connect(self) -> bool:
        self.disconnected.clear()
        self.controller = WalkingPadController(ble_device=self.device)
        self.controller.register_status_callback(self.on_status)
        self.controller.register_disconnect_callback(self.on_disconnect)
        try:
            await self.controller.connect()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("connect raised: %s", err)
            return False
        return self.controller.connected

    async def reconnect(self) -> bool:
        _LOGGER.info("Attempting reconnect…")
        try:
            if self.controller and self.controller.connected:
                await self.controller.disconnect()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2.0)
        # Re-discover (the BLEDevice address may still be valid but a fresh
        # scan tends to be more reliable on flaky firmware)
        device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10.0)
        if device is None:
            _LOGGER.error("Reconnect: device not found in scan")
            return False
        self.device = device
        return await self.connect()

    async def run_cycle(self, cycle_num: int, pattern: str, gap: float) -> CycleResult:
        result = CycleResult(cycle=cycle_num, pattern=pattern, gap_before_start=gap)

        c = self.controller
        if c is None or not c.connected:
            result.notes.append("not-connected-before-start")
            ok = await self.reconnect()
            result.notes.append(f"reconnect-{'ok' if ok else 'fail'}")
            if not ok:
                return result
            c = self.controller
            assert c is not None

        _LOGGER.info(
            "[cycle %d/%s gap=%.1fs] starting…", cycle_num, pattern, gap
        )

        # Start
        t = time.time()
        started = await c.start()
        result.start_ok = started and c.connected
        _LOGGER.info(
            "[cycle %d] start() = %s in %.2fs (speed=%.2f)",
            cycle_num,
            started,
            time.time() - t,
            self.last_status.speed if self.last_status else -1,
        )
        if not result.start_ok:
            result.notes.append(
                f"start-failed (returned={started}, connected={c.connected})"
            )
            return result

        # Set a speed (simulates user slider)
        t = time.time()
        speed_ok = await c.set_speed(2.5)
        result.set_speed_ok = speed_ok and c.connected
        _LOGGER.info(
            "[cycle %d] set_speed(2.5) = %s in %.2fs",
            cycle_num,
            speed_ok,
            time.time() - t,
        )
        if not result.set_speed_ok:
            result.notes.append("set-speed-failed")

        # Run for a bit
        await asyncio.sleep(RUN_DURATION)

        if self.disconnected.is_set():
            result.disconnected = True
            result.notes.append("disconnect-during-run")
            return result

        # Stop
        t = time.time()
        stopped = await c.stop()
        result.stop_ok = stopped and c.connected
        _LOGGER.info(
            "[cycle %d] stop() = %s in %.2fs",
            cycle_num,
            stopped,
            time.time() - t,
        )
        if not result.stop_ok:
            result.notes.append(
                f"stop-failed (returned={stopped}, connected={c.connected})"
            )

        if self.disconnected.is_set():
            result.disconnected = True
            result.notes.append("disconnect-after-stop")

        return result

    def report(self) -> int:
        _LOGGER.info("=" * 60)
        _LOGGER.info("RESULTS")
        _LOGGER.info("=" * 60)
        failed = 0
        for r in self.results:
            ok = (
                r.start_ok
                and r.set_speed_ok
                and r.stop_ok
                and not r.disconnected
                and not r.notes
            )
            tag = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            _LOGGER.info(
                "  [%s] cycle %2d %-7s gap=%4.1fs  start=%-5s speed=%-5s stop=%-5s disc=%-5s %s",
                tag,
                r.cycle,
                r.pattern,
                r.gap_before_start,
                r.start_ok,
                r.set_speed_ok,
                r.stop_ok,
                r.disconnected,
                "; ".join(r.notes) if r.notes else "",
            )
        _LOGGER.info("=" * 60)
        _LOGGER.info("Total: %d cycles, %d failed", len(self.results), failed)
        return failed


async def main() -> None:
    device = await find_device()
    rig = TestRig(device)

    if not await rig.connect():
        _LOGGER.error("Initial connect failed")
        sys.exit(1)

    assert rig.controller is not None
    assert rig.controller.protocol == ProtocolType.FTMS
    _LOGGER.info(
        "Connected. has_vendor_preamble=%s",
        rig.controller._ftms._capabilities.has_vendor_preamble,
    )
    await asyncio.sleep(2.0)

    cycle_n = 0
    for pattern, gap in GAP_PATTERNS:
        for _ in range(CYCLES_PER_PATTERN):
            cycle_n += 1
            r = await rig.run_cycle(cycle_n, pattern, gap)
            rig.results.append(r)
            # Apply gap before the NEXT cycle (i.e., gap between this stop
            # and next start)
            if r.disconnected or not rig.controller.connected:
                _LOGGER.warning("Disconnected; reconnecting before next cycle")
                ok = await rig.reconnect()
                if not ok:
                    _LOGGER.error("Could not reconnect, aborting")
                    rig.report()
                    sys.exit(1)
            await asyncio.sleep(gap)

    if rig.controller and rig.controller.connected:
        try:
            await rig.controller.stop()
            await asyncio.sleep(2.0)
            await rig.controller.disconnect()
        except Exception:  # noqa: BLE001
            pass

    failed = rig.report()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
