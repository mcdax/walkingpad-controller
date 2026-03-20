"""WiLink (legacy) protocol wrapper around ph4-walkingpad.

This module wraps the ph4_walkingpad.pad.Controller to provide the same
interface as FTMSController, using the unified TreadmillStatus model.

The ph4-walkingpad package is an optional dependency. If not installed,
importing this module will raise ImportError.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from bleak.backends.device import BLEDevice

from .models import TreadmillStatus

_LOGGER = logging.getLogger(__name__)


class WiLinkController:
    """Controller for legacy WalkingPad devices using the WiLink protocol.

    Wraps ph4_walkingpad.pad.Controller to provide a consistent API.
    Communicates over BLE service 0xFE00.
    """

    def __init__(self) -> None:
        try:
            from ph4_walkingpad.pad import Controller

            self._controller = Controller()
            self._controller.log_messages_info = False
        except ImportError:
            raise ImportError(
                "ph4-walkingpad is required for legacy WiLink devices. "
                "Install it with: pip install walkingpad-controller[wilink]"
            )

        self._connected = False
        self._status = TreadmillStatus()
        self._status_callbacks: list[Callable[[TreadmillStatus], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []

        # Register our status handler on the ph4 controller
        self._controller.handler_cur_status = self._on_status_update

    @property
    def connected(self) -> bool:
        """Return whether the device is connected."""
        return self._connected

    @property
    def status(self) -> TreadmillStatus:
        """Return the current treadmill status."""
        return self._status

    @property
    def min_speed(self) -> float:
        """Minimum speed in km/h (fixed for legacy devices)."""
        return 0.5

    @property
    def max_speed(self) -> float:
        """Maximum speed in km/h (fixed for legacy devices)."""
        return 6.0

    @property
    def speed_increment(self) -> float:
        """Speed increment in km/h (fixed for legacy devices)."""
        return 0.1

    def register_status_callback(
        self, callback: Callable[[TreadmillStatus], None]
    ) -> None:
        """Register a callback for status updates."""
        self._status_callbacks.append(callback)

    def register_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for disconnect events."""
        self._disconnect_callbacks.append(callback)

    def _on_status_update(self, sender, data) -> None:
        """Handle status update from ph4_walkingpad Controller."""
        from .const import BeltState, OperatingMode

        belt_state = (
            data.belt_state
            if data.belt_state in (e.value for e in BeltState)
            else BeltState.UNKNOWN
        )

        self._status = TreadmillStatus(
            belt_state=belt_state,
            speed=data.speed / 10.0,
            mode=data.manual_mode,
            distance=data.dist * 10,  # ph4 reports in 1/100 km, we want meters
            duration=data.time,
            steps=data.steps,
            calories=0,  # Not available via WiLink protocol
            calories_per_hour=0,
            heart_rate=0,
            timestamp=time.time(),
        )

        for cb in self._status_callbacks:
            try:
                cb(self._status)
            except Exception:
                _LOGGER.exception("Error in status callback")

    # --- Connection ---

    async def connect(self, ble_device: BLEDevice) -> None:
        """Connect to the WiLink treadmill.

        Args:
            ble_device: The BLE device to connect to.
        """
        _LOGGER.info("WiLink: Connecting to %s", ble_device.address)
        await self._controller.run(ble_device)
        self._connected = True
        _LOGGER.info("WiLink: Connected to %s", ble_device.address)

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        try:
            await self._controller.disconnect()
        except Exception:
            _LOGGER.exception("Error during WiLink disconnect")
        finally:
            self._connected = False

    # --- Commands ---

    async def start(self, target_speed: float | None = None) -> bool:
        """Start the treadmill belt.

        Args:
            target_speed: Optional target speed in km/h. If provided and the
                belt starts successfully, the speed will be set afterward.

        Returns:
            True if the command was sent successfully.
        """
        try:
            await self._controller.start_belt()
            if target_speed is not None:
                speed_tenths = int(target_speed * 10)
                await self._controller.change_speed(speed_tenths)
            return True
        except Exception as err:
            _LOGGER.warning("WiLink: Start failed: %s", err)
            return False

    async def stop(self) -> bool:
        """Stop the treadmill belt."""
        try:
            await self._controller.stop_belt()
            return True
        except Exception as err:
            _LOGGER.warning("WiLink: Stop failed: %s", err)
            return False

    async def set_target_speed(self, speed_kmh: float) -> bool:
        """Set the belt speed in km/h.

        Args:
            speed_kmh: Target speed in km/h.

        Returns:
            True if the command was sent successfully.
        """
        try:
            speed_tenths = int(speed_kmh * 10)
            await self._controller.change_speed(speed_tenths)
            return True
        except Exception as err:
            _LOGGER.warning("WiLink: Set speed failed: %s", err)
            return False

    async def switch_mode(self, mode: int) -> bool:
        """Switch the treadmill operating mode.

        Args:
            mode: Operating mode (0=auto, 1=manual, 2=standby).

        Returns:
            True if the command was sent successfully.
        """
        try:
            await self._controller.switch_mode(mode)
            return True
        except Exception as err:
            _LOGGER.warning("WiLink: Switch mode failed: %s", err)
            return False

    async def ask_stats(self) -> None:
        """Request current status from the treadmill."""
        try:
            await self._controller.ask_stats()
        except Exception as err:
            _LOGGER.warning("WiLink: Ask stats failed: %s", err)
