"""Unified WalkingPad treadmill controller with auto protocol detection.

This is the main entry point for controlling WalkingPad/KingSmith treadmills.
It auto-detects the BLE protocol (FTMS or WiLink) and delegates to the
appropriate backend.

Example usage:

    from bleak import BleakScanner
    from walkingpad_controller import WalkingPadController

    device = await BleakScanner.find_device_by_name("KS-HD-Z1D")
    controller = WalkingPadController(ble_device=device)
    await controller.connect()

    # Start the belt (runs at minimum speed)
    await controller.start()

    # Set desired speed via the speed slider / set_speed()
    await controller.set_speed(3.0)

    # Get status
    print(controller.status)

    # Stop
    await controller.stop()
    await controller.disconnect()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .const import (
    FTMS_NAME_PREFIXES,
    FTMS_SERVICE_UUID,
    MAX_CONNECT_RETRIES,
    RETRY_DELAY_SECONDS,
    WILINK_SERVICE_UUID,
    OperatingMode,
    ProtocolType,
)
from .ftms import FTMSController
from .models import TreadmillStatus

_LOGGER = logging.getLogger(__name__)


class WalkingPadController:
    """Unified WalkingPad treadmill controller.

    Auto-detects the BLE protocol on first connection and delegates to
    either FTMSController or WiLinkController.

    Args:
        ble_device: The BLE device to control.
        name: Optional friendly name for logging.
    """

    def __init__(self, ble_device: BLEDevice, name: str | None = None) -> None:
        self._ble_device = ble_device
        self._name = name or ble_device.name or ble_device.address
        self._protocol: ProtocolType = ProtocolType.UNKNOWN
        self._connected = False
        self._lock = asyncio.Lock()

        # Protocol backends
        self._ftms: FTMSController | None = None
        self._wilink = None  # WiLinkController (lazy import)

        # Status callbacks
        self._status_callbacks: list[Callable[[TreadmillStatus], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []

        # Eagerly detect protocol from BLE name
        name_protocol = self._detect_protocol_from_name()
        if name_protocol is not None:
            self._protocol = name_protocol

    # --- Properties ---

    @property
    def name(self) -> str:
        """Device name."""
        return self._name

    @property
    def address(self) -> str:
        """BLE MAC address."""
        return self._ble_device.address

    @property
    def protocol(self) -> ProtocolType:
        """The detected or configured protocol type."""
        return self._protocol

    @property
    def connected(self) -> bool:
        """Whether the device is currently connected."""
        return self._connected

    @property
    def status(self) -> TreadmillStatus:
        """Current treadmill status."""
        if self._ftms:
            return self._ftms.status
        if self._wilink:
            return self._wilink.status
        return TreadmillStatus()

    @property
    def min_speed(self) -> float:
        """Minimum speed in km/h."""
        if self._ftms:
            return self._ftms.min_speed
        if self._wilink:
            return self._wilink.min_speed
        return 0.5

    @property
    def max_speed(self) -> float:
        """Maximum speed in km/h."""
        if self._ftms:
            return self._ftms.max_speed
        if self._wilink:
            return self._wilink.max_speed
        return 6.0

    @property
    def speed_increment(self) -> float:
        """Speed increment in km/h."""
        if self._ftms:
            return self._ftms.speed_increment
        if self._wilink:
            return self._wilink.speed_increment
        return 0.1

    # --- Callbacks ---

    def register_status_callback(
        self, callback: Callable[[TreadmillStatus], None]
    ) -> None:
        """Register a callback for status updates.

        The callback receives a TreadmillStatus object whenever the device
        reports new data (via FTMS notifications or WiLink polling).
        """
        self._status_callbacks.append(callback)

    def register_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for disconnect events."""
        self._disconnect_callbacks.append(callback)

    def _on_status_update(self, status: TreadmillStatus) -> None:
        """Internal handler for status updates from either backend."""
        for cb in self._status_callbacks:
            try:
                cb(status)
            except Exception:
                _LOGGER.exception("Error in status callback")

    def _on_disconnect(self) -> None:
        """Internal handler for disconnect events from either backend."""
        _LOGGER.warning("Device disconnected")
        self._connected = False
        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in disconnect callback")

    # --- Protocol Detection ---

    def _detect_protocol_from_name(self) -> ProtocolType | None:
        """Detect protocol from the BLE device name.

        Returns None if the name doesn't give a definitive answer.
        """
        ble_name = self._ble_device.name or ""
        for prefix in FTMS_NAME_PREFIXES:
            if ble_name.startswith(prefix):
                _LOGGER.info(
                    "Detected FTMS protocol from BLE name '%s' (prefix '%s')",
                    ble_name,
                    prefix,
                )
                return ProtocolType.FTMS
        return None

    def _detect_protocol_from_services(self, service_uuids: set[str]) -> ProtocolType:
        """Determine the protocol based on discovered service UUIDs."""
        has_ftms = FTMS_SERVICE_UUID.lower() in service_uuids
        has_wilink = WILINK_SERVICE_UUID.lower() in service_uuids

        if has_ftms and not has_wilink:
            _LOGGER.info("Detected FTMS protocol (no WiLink service)")
            return ProtocolType.FTMS
        elif has_wilink:
            _LOGGER.info("Detected legacy WiLink protocol")
            return ProtocolType.WILINK
        elif has_ftms:
            _LOGGER.info("Detected FTMS protocol (with WiLink fallback)")
            return ProtocolType.FTMS
        else:
            _LOGGER.warning("No known protocol detected")
            return ProtocolType.UNKNOWN

    async def _detect_protocol_from_probe(self) -> ProtocolType:
        """Detect protocol by probing BLE services."""
        _LOGGER.info("Probing protocol for %s", self._ble_device.address)
        try:
            async with BleakClient(self._ble_device) as client:
                service_uuids = {s.uuid.lower() for s in client.services}
                return self._detect_protocol_from_services(service_uuids)
        except (BleakError, TimeoutError) as err:
            _LOGGER.warning("Protocol detection failed: %s", err)
            return ProtocolType.UNKNOWN

    # --- Connection ---

    async def connect(self) -> None:
        """Connect to the device, auto-detecting protocol if needed.

        Raises:
            BleakError: If the BLE connection fails after all retries.
            RuntimeError: If the protocol cannot be determined.
        """
        async with self._lock:
            if self._connected:
                return

            _LOGGER.info("Connecting to %s (%s)", self._name, self._ble_device.address)

            # Detect protocol on first connection
            if self._protocol == ProtocolType.UNKNOWN:
                name_protocol = self._detect_protocol_from_name()
                if name_protocol is not None:
                    self._protocol = name_protocol
                else:
                    self._protocol = await self._detect_protocol_from_probe()

            if self._protocol == ProtocolType.FTMS:
                await self._connect_ftms()
            elif self._protocol == ProtocolType.WILINK:
                await self._connect_wilink()
            else:
                raise RuntimeError(
                    f"Unknown protocol for device {self._ble_device.address}"
                )

            self._connected = True
            _LOGGER.info("Connected via %s protocol", self._protocol.value)

    async def _connect_ftms(self) -> None:
        """Connect using the FTMS protocol with retry logic."""
        last_error: Exception | None = None
        for attempt in range(1, MAX_CONNECT_RETRIES + 1):
            try:
                self._ftms = FTMSController()
                self._ftms.register_status_callback(self._on_status_update)
                self._ftms.register_disconnect_callback(self._on_disconnect)
                await self._ftms.connect(self._ble_device)
                return
            except (BleakError, TimeoutError) as err:
                last_error = err
                _LOGGER.warning(
                    "FTMS connection attempt %d/%d failed: %s",
                    attempt,
                    MAX_CONNECT_RETRIES,
                    err,
                )
                if attempt < MAX_CONNECT_RETRIES:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
        raise last_error  # type: ignore[misc]

    async def _connect_wilink(self) -> None:
        """Connect using the legacy WiLink protocol."""
        from .wilink import WiLinkController

        self._wilink = WiLinkController()
        self._wilink.register_status_callback(self._on_status_update)
        self._wilink.register_disconnect_callback(self._on_disconnect)
        await self._wilink.connect(self._ble_device)

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        if not self._connected:
            return
        async with self._lock:
            try:
                if self._ftms:
                    await self._ftms.disconnect()
                elif self._wilink:
                    await self._wilink.disconnect()
            except Exception:
                _LOGGER.exception("Error during disconnect")
            finally:
                self._connected = False

    # --- Commands ---

    async def start(self) -> bool:
        """Start the treadmill belt.

        For FTMS devices, sends START_OR_RESUME and waits for the belt
        to begin moving.  Does NOT send SET_TARGET_SPEED — the user
        must set speed explicitly via set_speed() (e.g. the HA speed
        slider).  Sending a speed command during motor spin-up crashes
        the BLE connection on KingSmith firmware.

        For WiLink devices, sends the standard start command.

        Returns:
            True if the belt is running. False if the connection was lost.
        """
        if self._ftms:
            return await self._ftms.start()

        elif self._wilink:
            return await self._wilink.start()

        _LOGGER.warning("No protocol backend available")
        return False

    async def stop(self) -> bool:
        """Stop the treadmill.

        Returns:
            True if the command was sent successfully.
        """
        if self._ftms:
            return await self._ftms.stop()
        elif self._wilink:
            return await self._wilink.stop()

        _LOGGER.warning("No protocol backend available")
        return False

    async def set_speed(self, speed_kmh: float) -> bool:
        """Set the treadmill speed.

        If the belt is already running, sends SET_TARGET_SPEED directly.
        If the belt is stopped, starts it first (the belt will run at
        minimum speed until the user adjusts the speed slider).

        Args:
            speed_kmh: Target speed in km/h.

        Returns:
            True if the command was sent successfully.
        """
        if self._ftms:
            if self._ftms.status.speed > 0:
                # Belt already running — safe to send speed directly
                return await self._ftms.set_target_speed(speed_kmh)
            else:
                # Belt is stopped — start it first.  The user will need
                # to set the desired speed once the belt is running.
                _LOGGER.info(
                    "Belt is stopped — starting first, then setting speed %.1f",
                    speed_kmh,
                )
                started = await self._ftms.start()
                if not started:
                    return False
                # Belt is now running; send the speed command
                return await self._ftms.set_target_speed(speed_kmh)

        elif self._wilink:
            return await self._wilink.set_target_speed(speed_kmh)

        _LOGGER.warning("No protocol backend available")
        return False

    async def switch_mode(self, mode: OperatingMode) -> bool:
        """Switch the treadmill operating mode.

        FTMS devices don't support auto/manual modes natively.
        STANDBY maps to stop, AUTO maps to start at min speed.

        Args:
            mode: The target operating mode.

        Returns:
            True if the command was sent successfully.
        """
        if self._ftms:
            if mode == OperatingMode.STANDBY:
                return await self._ftms.stop()
            elif mode == OperatingMode.AUTO:
                return await self._ftms.start()
            return True  # MANUAL is the default FTMS state

        elif self._wilink:
            return await self._wilink.switch_mode(mode.value)

        _LOGGER.warning("No protocol backend available")
        return False

    async def update_state(self) -> None:
        """Request current state from the device.

        For FTMS devices, status is pushed via notifications so this
        fires a synthetic update from cached data. For WiLink devices,
        this polls the device.
        """
        if self._ftms:
            if self._ftms.connected:
                self._on_status_update(self._ftms.status)
            else:
                self._connected = False
        elif self._wilink:
            await self._wilink.ask_stats()

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLE device reference (e.g., after rediscovery).

        Args:
            ble_device: The new BLE device reference.
        """
        self._ble_device = ble_device
