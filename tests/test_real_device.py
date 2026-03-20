"""Test walkingpad-controller library against real KS-HD-Z1D device.

Tests the cold-start flow WITHOUT automatic SET_TARGET_SPEED:
  scan -> connect -> start (no speed) -> observe belt at min speed ->
  set_speed (user action) -> observe -> stop -> disconnect
"""

import asyncio
import logging
import sys
import time

from bleak import BleakScanner

from walkingpad_controller import WalkingPadController, TreadmillStatus, ProtocolType

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
_LOGGER = logging.getLogger("test")

DEVICE_NAME = "KS-HD-Z1D"
TARGET_SPEED = 2.0  # km/h — sent AFTER belt is running (simulates user slider)
RUN_DURATION = 20  # seconds to observe after setting speed


async def main():
    # --- Step 1: Scan ---
    _LOGGER.info("Scanning for %s...", DEVICE_NAME)
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if device is None:
        _LOGGER.error("Device %s not found!", DEVICE_NAME)
        sys.exit(1)
    _LOGGER.info("Found: %s (%s)", device.name, device.address)

    # --- Step 2: Create controller ---
    controller = WalkingPadController(ble_device=device)
    _LOGGER.info("Protocol detected from name: %s", controller.protocol.value)
    assert controller.protocol == ProtocolType.FTMS, (
        f"Expected FTMS, got {controller.protocol}"
    )

    # Register status callback
    status_count = 0

    def on_status(status: TreadmillStatus):
        nonlocal status_count
        status_count += 1
        if status_count % 5 == 1:  # Log every 5th update to reduce noise
            _LOGGER.info(
                "STATUS #%d: speed=%.2f km/h, dist=%dm, cal=%d, time=%ds, steps=%d, belt=%s",
                status_count,
                status.speed,
                status.distance,
                status.calories,
                status.duration,
                status.steps,
                status.belt_state,
            )

    disconnected = asyncio.Event()

    def on_disconnect():
        _LOGGER.warning("DISCONNECT callback fired!")
        disconnected.set()

    controller.register_status_callback(on_status)
    controller.register_disconnect_callback(on_disconnect)

    # --- Step 3: Connect ---
    _LOGGER.info("Connecting...")
    await controller.connect()
    _LOGGER.info("Connected! Protocol: %s", controller.protocol.value)
    _LOGGER.info(
        "Speed range: %.1f - %.1f km/h (step %.2f)",
        controller.min_speed,
        controller.max_speed,
        controller.speed_increment,
    )

    # --- Step 4: Read initial status ---
    _LOGGER.info("Waiting 3s for initial status notifications...")
    await asyncio.sleep(3)
    s = controller.status
    _LOGGER.info(
        "Initial status: speed=%.2f, belt=%s, dist=%d, cal=%d, time=%d, steps=%d",
        s.speed,
        s.belt_state,
        s.distance,
        s.calories,
        s.duration,
        s.steps,
    )

    # --- Step 5: Start (no target speed — belt runs at minimum) ---
    _LOGGER.info("Starting belt (no target speed)...")
    result = await controller.start()
    _LOGGER.info("start() returned: %s", result)

    if not controller.connected:
        _LOGGER.error("Connection lost during start! TEST FAILED.")
        return

    s = controller.status
    _LOGGER.info(
        "After start: speed=%.2f km/h (should be ~%.1f min speed)",
        s.speed,
        controller.min_speed,
    )

    # --- Step 6: Wait a moment, then set speed (simulates user slider) ---
    _LOGGER.info(
        "Belt running at min speed. Waiting 5s before setting speed to %.1f...",
        TARGET_SPEED,
    )
    await asyncio.sleep(5)

    if not controller.connected:
        _LOGGER.error("Connection lost while waiting! TEST FAILED.")
        return

    _LOGGER.info("Setting speed to %.1f km/h (simulates user slider)...", TARGET_SPEED)
    result = await controller.set_speed(TARGET_SPEED)
    _LOGGER.info("set_speed() returned: %s", result)

    if not controller.connected:
        _LOGGER.error("Connection lost after set_speed! TEST FAILED.")
        return

    # --- Step 7: Run and observe ---
    _LOGGER.info("Observing for %ds...", RUN_DURATION)
    start_time = time.time()
    while time.time() - start_time < RUN_DURATION:
        if disconnected.is_set():
            _LOGGER.error("Disconnected during run! TEST FAILED.")
            return
        await asyncio.sleep(1)

    s = controller.status
    _LOGGER.info(
        "After run: speed=%.2f, dist=%d, cal=%d, time=%d, steps=%d",
        s.speed,
        s.distance,
        s.calories,
        s.duration,
        s.steps,
    )

    # --- Step 8: Stop ---
    if controller.connected:
        _LOGGER.info("Stopping belt...")
        result = await controller.stop()
        _LOGGER.info("stop() returned: %s", result)
        await asyncio.sleep(3)
        s = controller.status
        _LOGGER.info("After stop: speed=%.2f, belt=%s", s.speed, s.belt_state)

    # --- Step 9: Disconnect ---
    _LOGGER.info("Disconnecting...")
    await controller.disconnect()
    _LOGGER.info("Disconnected. Total status updates received: %d", status_count)
    _LOGGER.info("TEST PASSED — no BLE disconnect during cold start!")


if __name__ == "__main__":
    asyncio.run(main())
