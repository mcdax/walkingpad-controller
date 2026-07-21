"""Diagnostic probe for the random-disconnect pattern.

Runs four scenarios back-to-back and times exactly when the BLE link
dies in each.  Each scenario is preceded by a clean reconnect.

A: idle           — connect, send nothing, wait for disconnect
B: belt running   — connect, start, set 2 km/h, then do nothing
C: post-stop      — connect, start, set speed, stop, then do nothing
D: post-stop+poll — connect, start, set speed, stop, then read battery
                    every second to keep traffic flowing

For each scenario we record every notification (handle, payload, dt)
and the time between the last action and the eventual disconnect.
If scenario D survives noticeably longer than C, the cause is "no
traffic → supervision-timeout."  If A survives indefinitely we've
ruled out plain idle-disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field

from bleak import BleakScanner

from walkingpad_controller import WalkingPadController, ProtocolType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("bleak").setLevel(logging.WARNING)
_LOGGER = logging.getLogger("probe")

DEVICE_NAME = "KS-HD-Z1D"
OBSERVATION = 60.0  # max seconds to wait per scenario before giving up


@dataclass
class Scenario:
    name: str
    description: str
    last_action_at: float = 0.0
    disconnect_at: float = 0.0
    notif_count: int = 0
    notif_intervals: list[float] = field(default_factory=list)
    last_notif_at: float = 0.0
    cmd_log: list[str] = field(default_factory=list)
    completed: bool = False
    disconnected: bool = False


async def find_device():
    _LOGGER.info("Scanning %s…", DEVICE_NAME)
    d = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if d is None:
        _LOGGER.error("Device not found")
        sys.exit(1)
    return d


async def connect_with_capture(scenario: Scenario):
    device = await find_device()
    controller = WalkingPadController(ble_device=device)
    assert controller.protocol == ProtocolType.FTMS

    disconnect_event = asyncio.Event()

    def on_status(status):
        now = time.time()
        if scenario.last_notif_at:
            scenario.notif_intervals.append(now - scenario.last_notif_at)
        scenario.last_notif_at = now
        scenario.notif_count += 1

    def on_disc():
        scenario.disconnect_at = time.time()
        scenario.disconnected = True
        disconnect_event.set()

    controller.register_status_callback(on_status)
    controller.register_disconnect_callback(on_disc)

    await controller.connect()
    if not controller.connected:
        _LOGGER.error("Initial connect failed for scenario %s", scenario.name)
        return None, None
    return controller, disconnect_event


async def wait_for_disconnect(scenario: Scenario, disconnect_event: asyncio.Event, timeout: float):
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        if disconnect_event.is_set():
            elapsed = scenario.disconnect_at - scenario.last_action_at
            _LOGGER.warning(
                "  -> DISCONNECTED after %.1fs of idle (scenario %s)",
                elapsed,
                scenario.name,
            )
            return
        # heartbeat
        now = time.time()
        if now - last_log > 5:
            elapsed = now - scenario.last_action_at
            _LOGGER.info(
                "  scenario %s alive: %.0fs since last action, notifs=%d",
                scenario.name,
                elapsed,
                scenario.notif_count,
            )
            last_log = now
        await asyncio.sleep(0.5)
    scenario.completed = True
    _LOGGER.info(
        "  scenario %s SURVIVED %.0fs (notifs=%d) — no disconnect",
        scenario.name,
        timeout,
        scenario.notif_count,
    )


async def run_scenario_a():
    """Connect, do nothing."""
    s = Scenario(name="A", description="idle, no actions")
    _LOGGER.info("=== Scenario A: %s ===", s.description)
    controller, disc = await connect_with_capture(s)
    if controller is None:
        return s
    s.last_action_at = time.time()
    s.cmd_log.append("connect")
    await wait_for_disconnect(s, disc, OBSERVATION)
    try:
        if controller.connected:
            await controller.disconnect()
    except Exception:
        pass
    return s


async def run_scenario_b():
    """Connect, start, set 2 km/h, leave belt running."""
    s = Scenario(name="B", description="belt running, then idle")
    _LOGGER.info("=== Scenario B: %s ===", s.description)
    controller, disc = await connect_with_capture(s)
    if controller is None:
        return s
    s.cmd_log.append("connect")
    await controller.start()
    s.cmd_log.append("start")
    await controller.set_speed(2.0)
    s.cmd_log.append("set_speed 2.0")
    s.last_action_at = time.time()
    await wait_for_disconnect(s, disc, OBSERVATION)
    try:
        if controller.connected:
            await controller.stop()
            await controller.disconnect()
    except Exception:
        pass
    return s


async def run_scenario_c():
    """Connect, start, set speed, stop, do nothing — replicates 'after-stop' bug."""
    s = Scenario(name="C", description="post-stop, no further traffic")
    _LOGGER.info("=== Scenario C: %s ===", s.description)
    controller, disc = await connect_with_capture(s)
    if controller is None:
        return s
    s.cmd_log.append("connect")
    await controller.start()
    s.cmd_log.append("start")
    await controller.set_speed(2.0)
    s.cmd_log.append("set_speed 2.0")
    await asyncio.sleep(3)
    await controller.stop()
    s.cmd_log.append("stop")
    s.last_action_at = time.time()
    await wait_for_disconnect(s, disc, OBSERVATION)
    try:
        if controller.connected:
            await controller.disconnect()
    except Exception:
        pass
    return s


async def run_scenario_d():
    """Connect, start, set speed, stop, then poll something every 1s."""
    s = Scenario(name="D", description="post-stop, periodic 1Hz read")
    _LOGGER.info("=== Scenario D: %s ===", s.description)
    controller, disc = await connect_with_capture(s)
    if controller is None:
        return s
    s.cmd_log.append("connect")
    await controller.start()
    s.cmd_log.append("start")
    await controller.set_speed(2.0)
    s.cmd_log.append("set_speed 2.0")
    await asyncio.sleep(3)
    await controller.stop()
    s.cmd_log.append("stop")
    s.last_action_at = time.time()

    deadline = time.time() + OBSERVATION
    while time.time() < deadline and not disc.is_set():
        try:
            # Read Battery Level (a benign characteristic) — keeps traffic flowing.
            await controller._ftms._client.read_gatt_char(
                "00002a19-0000-1000-8000-00805f9b34fb"
            )
        except Exception as err:
            _LOGGER.warning("  read_gatt_char failed: %s", err)
            break
        await asyncio.sleep(1.0)
    if disc.is_set():
        elapsed = s.disconnect_at - s.last_action_at
        _LOGGER.warning(
            "  -> DISCONNECTED after %.1fs while polling 1Hz", elapsed
        )
    else:
        s.completed = True
        _LOGGER.info(
            "  scenario %s SURVIVED %.0fs of 1Hz polling — no disconnect",
            s.name,
            OBSERVATION,
        )
    try:
        if controller.connected:
            await controller.disconnect()
    except Exception:
        pass
    return s


async def main():
    results: list[Scenario] = []
    for runner in (run_scenario_a, run_scenario_b, run_scenario_c, run_scenario_d):
        try:
            r = await runner()
            results.append(r)
        except Exception as err:
            _LOGGER.exception("scenario raised: %s", err)
        # back off between scenarios so the device fully releases the link
        await asyncio.sleep(8)

    _LOGGER.info("=" * 70)
    _LOGGER.info("SUMMARY")
    _LOGGER.info("=" * 70)
    for s in results:
        if s.disconnected:
            elapsed = s.disconnect_at - s.last_action_at
            verdict = f"disconnected after {elapsed:.1f}s"
        elif s.completed:
            verdict = f"survived {OBSERVATION:.0f}s"
        else:
            verdict = "did not run"
        _LOGGER.info(
            "  %s | %-32s | notifs=%-3d | %s",
            s.name,
            s.description,
            s.notif_count,
            verdict,
        )


if __name__ == "__main__":
    asyncio.run(main())
