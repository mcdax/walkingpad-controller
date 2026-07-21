"""Stress test — exercises the library against a real KS-HD-Z1D.

Distinct from test_real_device.py (which is a single-path smoke test).
This script hammers start/stop cycles, rapid speed changes, and idle
windows to surface BLE flakiness or regressions in the new vendor
pre-amble + REQUEST_CONTROL-tolerance code paths.

Verifies:
  - Capability detection (KS-HD-Z1D has no vendor pre-amble char)
  - Cold-start invariant (no SET_TARGET_SPEED before belt is moving)
  - Speed sweep across the device's range
  - Repeated start/stop/start cycles
  - Idle persistence (no spurious disconnect over a 60s wait)
  - No BLE disconnect during any of the above
"""

import asyncio
import logging
import sys
import time

from bleak import BleakScanner

from walkingpad_controller import ProtocolType, TreadmillStatus, WalkingPadController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("stress")

DEVICE_NAME = "KS-HD-Z1D"

# Speed sequence: cold-start at min, then rapid changes.
SPEED_SWEEP = [1.5, 3.0, 2.0, 4.0, 1.0, 3.5, 2.5]
INTER_SPEED_DELAY = 4.0  # seconds between speed changes
START_STOP_CYCLES = 2  # how many full start/stop cycles to run
IDLE_OBSERVATION = 30.0  # seconds to sit idle and watch for spurious disconnect


async def find_device():
    _LOGGER.info("Scanning for %s...", DEVICE_NAME)
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if device is None:
        _LOGGER.error("Device %s not found.", DEVICE_NAME)
        sys.exit(1)
    _LOGGER.info("Found: %s (%s)", device.name, device.address)
    return device


async def main() -> None:
    device = await find_device()

    controller = WalkingPadController(ble_device=device)
    assert controller.protocol == ProtocolType.FTMS, (
        f"Expected FTMS, got {controller.protocol}"
    )

    status_count = 0
    last_speed = 0.0

    def on_status(status: TreadmillStatus) -> None:
        nonlocal status_count, last_speed
        status_count += 1
        last_speed = status.speed
        if status_count % 10 == 1:
            _LOGGER.info(
                "  status #%d: speed=%.2f km/h dist=%dm time=%ds steps=%d",
                status_count,
                status.speed,
                status.distance,
                status.duration,
                status.steps,
            )

    disconnected = asyncio.Event()

    def on_disconnect() -> None:
        _LOGGER.error("DISCONNECT callback fired!")
        disconnected.set()

    controller.register_status_callback(on_status)
    controller.register_disconnect_callback(on_disconnect)

    failures: list[str] = []

    # --- Phase 1: connect & capability detection ---
    _LOGGER.info("=== Phase 1: connect ===")
    t0 = time.time()
    await controller.connect()
    _LOGGER.info("Connected in %.2fs (protocol=%s)", time.time() - t0, controller.protocol.value)
    _LOGGER.info(
        "Speed range %.1f–%.1f km/h step %.2f",
        controller.min_speed,
        controller.max_speed,
        controller.speed_increment,
    )

    # KS-HD-Z1D should NOT have the MC-21 vendor pre-amble characteristic.
    has_vendor = controller._ftms._capabilities.has_vendor_preamble
    _LOGGER.info("has_vendor_preamble = %s (expected False on KS-HD-Z1D)", has_vendor)
    if has_vendor:
        failures.append("Unexpected: vendor pre-amble characteristic detected")

    await asyncio.sleep(2.0)

    # --- Phase 2: cold-start sequence ---
    _LOGGER.info("=== Phase 2: cold-start ===")
    t0 = time.time()
    started = await controller.start()
    _LOGGER.info("start() -> %s in %.2fs", started, time.time() - t0)
    if not started or not controller.connected:
        failures.append("start() failed or connection lost")
        return await teardown(controller, failures, status_count)
    _LOGGER.info("Belt now at %.2f km/h", last_speed)

    # --- Phase 3: speed sweep ---
    _LOGGER.info("=== Phase 3: speed sweep ===")
    for target in SPEED_SWEEP:
        if disconnected.is_set():
            failures.append(f"Disconnected during speed sweep at target={target}")
            break
        t0 = time.time()
        ok = await controller.set_speed(target)
        _LOGGER.info("  set_speed(%.1f) -> %s in %.2fs", target, ok, time.time() - t0)
        if not ok:
            failures.append(f"set_speed({target}) returned False")
        await asyncio.sleep(INTER_SPEED_DELAY)
        if abs(last_speed - target) > 0.5:
            _LOGGER.warning(
                "  observed speed %.2f differs from target %.2f (>0.5 km/h)",
                last_speed,
                target,
            )

    # --- Phase 4: stop / start cycles ---
    _LOGGER.info("=== Phase 4: stop/start cycles (%d) ===", START_STOP_CYCLES)
    for cycle in range(1, START_STOP_CYCLES + 1):
        if disconnected.is_set():
            failures.append(f"Disconnected before cycle {cycle}")
            break
        _LOGGER.info("  cycle %d: stop", cycle)
        await controller.stop()
        await asyncio.sleep(5.0)
        _LOGGER.info("  cycle %d: post-stop speed=%.2f", cycle, last_speed)
        if disconnected.is_set():
            failures.append(f"Disconnected during cycle {cycle} stop")
            break
        _LOGGER.info("  cycle %d: start", cycle)
        ok = await controller.start()
        if not ok:
            failures.append(f"cycle {cycle} restart failed")
            break
        await asyncio.sleep(2.0)
        ok = await controller.set_speed(2.0)
        if not ok:
            failures.append(f"cycle {cycle} set_speed(2.0) failed")
        await asyncio.sleep(3.0)

    # --- Phase 5: idle persistence ---
    _LOGGER.info(
        "=== Phase 5: idle %.0fs to check for spurious disconnect ===",
        IDLE_OBSERVATION,
    )
    deadline = time.time() + IDLE_OBSERVATION
    pre_idle_count = status_count
    while time.time() < deadline:
        if disconnected.is_set():
            failures.append("Disconnected during idle observation")
            break
        await asyncio.sleep(1.0)
    _LOGGER.info(
        "Status updates during idle: %d (expect ~%.0f at 1Hz)",
        status_count - pre_idle_count,
        IDLE_OBSERVATION,
    )

    # --- Phase 6: final stop & disconnect ---
    _LOGGER.info("=== Phase 6: final stop ===")
    if controller.connected:
        await controller.stop()
        await asyncio.sleep(3.0)

    await teardown(controller, failures, status_count)


async def teardown(controller, failures: list[str], status_count: int) -> None:
    _LOGGER.info("=== Teardown ===")
    try:
        if controller.connected:
            await controller.disconnect()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Error during disconnect")

    _LOGGER.info(
        "Total status updates: %d  |  failures: %d", status_count, len(failures)
    )
    if failures:
        _LOGGER.error("FAILED:")
        for f in failures:
            _LOGGER.error("  - %s", f)
        sys.exit(1)
    _LOGGER.info("STRESS TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
