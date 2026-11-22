"""Unified WalkingPad treadmill controller with auto protocol detection.

This is the main entry point for controlling WalkingPad/KingSmith treadmills.
It auto-detects the BLE protocol (FTMS, legacy WiLink or Sperax/WLT6200)
and delegates to the matching backend.

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
from bleak_retry_connector import establish_connection

from ._callbacks import fire_callbacks
from .const import (
    FTMS_NAME_PREFIXES,
    FTMS_SERVICE_UUID,
    MAX_CONNECT_RETRIES,
    RETRY_DELAY_SECONDS,
    SPERAX_NAME_PREFIXES,
    SPERAX_SERVICE_UUID,
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
    FTMSController, WiLinkController or SperaxController.

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
        self._sperax = None  # SperaxController (lazy import)

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
    def _backend(self):
        """The active protocol backend, or None before connecting.

        Only one backend is ever set: each `_connect_*` sets its own after
        `connect()` cleared all three via `_close_backend`. Backends define
        no ``__bool__``, so ``or`` picks the first one that is not None.
        """
        return self._ftms or self._wilink or self._sperax

    @property
    def connected(self) -> bool:
        """Whether the device is currently connected.

        Defers to the active backend so the result reflects the live BLE
        state, not just a cached bool that can drift if the firmware
        unilaterally drops the link before the disconnect callback fires.
        """
        backend = self._backend
        return backend.connected if backend is not None else self._connected

    @property
    def status(self) -> TreadmillStatus:
        """Current treadmill status."""
        backend = self._backend
        return backend.status if backend is not None else TreadmillStatus()

    @property
    def min_speed(self) -> float:
        """Minimum speed in km/h."""
        backend = self._backend
        return backend.min_speed if backend is not None else 0.5

    @property
    def max_speed(self) -> float:
        """Maximum speed in km/h."""
        backend = self._backend
        return backend.max_speed if backend is not None else 6.0

    @property
    def speed_increment(self) -> float:
        """Speed increment in km/h."""
        backend = self._backend
        return backend.speed_increment if backend is not None else 0.1

    @property
    def firmware_version(self) -> str:
        """Firmware string, or empty if unavailable.

        Read from Software Revision String (`0x2A28`) on FTMS devices.
        Returns an empty string for WiLink and Sperax devices (not implemented).
        """
        if self._ftms:
            return self._ftms.firmware_version
        return ""

    # --- Callbacks ---

    def register_status_callback(
        self, callback: Callable[[TreadmillStatus], None]
    ) -> None:
        """Register a callback for status updates.

        The callback receives a TreadmillStatus object whenever the device
        reports new data (FTMS notifications, WiLink polling or Sperax
        polled status).
        """
        self._status_callbacks.append(callback)

    def register_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for disconnect events."""
        self._disconnect_callbacks.append(callback)

    def _on_status_update(self, status: TreadmillStatus) -> None:
        """Internal handler for status updates from the backend."""
        fire_callbacks(
            self._status_callbacks, _LOGGER, "Error in status callback", status
        )

    def _on_disconnect(self) -> None:
        """Internal handler for disconnect events from the backend."""
        _LOGGER.warning("Device disconnected")
        self._connected = False
        fire_callbacks(self._disconnect_callbacks, _LOGGER, "Error in disconnect callback")

    # --- Protocol Detection ---

    def _detect_protocol_from_name(self) -> ProtocolType | None:
        """Detect protocol from the BLE device name.

        Prefers the live ``BLEDevice.name`` but falls back to the configured
        name (``self._name``, e.g. the name stored by the Home Assistant config
        entry). On a restart the BT proxy often hands over a ``BLEDevice`` with
        no name yet; without this fallback the protocol would read UNKNOWN at
        setup, hiding protocol-specific entities until the device is re-added.
        The fallback resolves to the MAC address when no name exists, which
        matches no prefix, so there is no risk of a false positive.

        Returns None if the name doesn't give a definitive answer.
        """
        ble_name = self._ble_device.name or self._name or ""
        for prefix in FTMS_NAME_PREFIXES:
            if ble_name.startswith(prefix):
                _LOGGER.info(
                    "Detected FTMS protocol from BLE name '%s' (prefix '%s')",
                    ble_name,
                    prefix,
                )
                return ProtocolType.FTMS
        for prefix in SPERAX_NAME_PREFIXES:
            if ble_name.startswith(prefix):
                _LOGGER.info(
                    "Detected Sperax protocol from BLE name '%s' (prefix '%s')",
                    ble_name,
                    prefix,
                )
                return ProtocolType.SPERAX
        return None

    def _detect_protocol_from_services(self, service_uuids: set[str]) -> ProtocolType:
        """Determine the protocol based on discovered service UUIDs."""
        has_ftms = FTMS_SERVICE_UUID.lower() in service_uuids
        has_wilink = WILINK_SERVICE_UUID.lower() in service_uuids
        has_sperax = SPERAX_SERVICE_UUID.lower() in service_uuids

        if has_ftms and not has_wilink:
            _LOGGER.info("Detected FTMS protocol (no WiLink service)")
            return ProtocolType.FTMS
        elif has_wilink:
            _LOGGER.info("Detected legacy WiLink protocol")
            return ProtocolType.WILINK
        elif has_sperax:
            _LOGGER.info("Detected Sperax protocol (service 0xFFF0)")
            return ProtocolType.SPERAX
        else:
            _LOGGER.warning("No known protocol detected")
            return ProtocolType.UNKNOWN

    async def _detect_protocol_from_probe(self) -> ProtocolType:
        """Detect protocol by probing BLE services."""
        _LOGGER.info("Probing protocol for %s", self._ble_device.address)
        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClient,
                self._ble_device,
                self._name,
            )
            service_uuids = {s.uuid.lower() for s in client.services}
            return self._detect_protocol_from_services(service_uuids)
        except (BleakError, TimeoutError) as err:
            _LOGGER.warning("Protocol detection failed: %s", err)
            return ProtocolType.UNKNOWN
        finally:
            if client is not None and client.is_connected:
                try:
                    await client.disconnect()
                except BleakError:
                    pass

    # --- Connection ---

    async def connect(self) -> None:
        """Connect to the device, auto-detecting protocol if needed.

        Raises:
            BleakError: If the BLE connection fails after all retries.
            RuntimeError: If the protocol cannot be determined.
        """
        async with self._lock:
            if self.connected:
                return
            # The cached flag can be stale after a drop the backend didn't
            # report; close any leftover backend before connecting again.
            await self._close_backend()

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
            elif self._protocol == ProtocolType.SPERAX:
                await self._connect_sperax()
            else:
                raise RuntimeError(
                    f"Unknown protocol for device {self._ble_device.address}"
                )

            self._connected = True
            _LOGGER.info("Connected via %s protocol", self._protocol.value)

    async def _close_backend(self) -> None:
        """Disconnect and forget the current backend, if any."""
        backend = self._backend
        self._ftms = self._wilink = self._sperax = None
        self._connected = False
        if backend is not None:
            # Bleak reports explicit disconnects too; the old backend must
            # not tell our users that the new link dropped, nor forward a
            # late notification as the new link's status.
            backend._disconnect_callbacks.clear()
            backend._status_callbacks.clear()
            try:
                await backend.disconnect()
            except Exception:
                _LOGGER.debug("Error closing previous backend", exc_info=True)

    async def _connect_with_retries(self, label: str, factory, connect):
        """Connect a fresh backend from ``factory``, retrying failed attempts.

        ``connect(backend)`` performs the backend's connect call. Status
        callbacks are registered before connecting; disconnect callbacks
        only once the attempt succeeded, so a failed attempt's link can't
        report a disconnect later. Returns the connected backend.
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_CONNECT_RETRIES + 1):
            backend = factory()
            backend.register_status_callback(self._on_status_update)
            try:
                await connect(backend)
            except (BleakError, TimeoutError) as err:
                last_error = err
                _LOGGER.warning(
                    "%s connection attempt %d/%d failed: %s",
                    label,
                    attempt,
                    MAX_CONNECT_RETRIES,
                    err,
                )
                if attempt < MAX_CONNECT_RETRIES:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            if not backend.connected:
                # Dropped between setup and here; close it and retry.
                await backend.disconnect()
                last_error = BleakError(f"{label}: link dropped right after connecting")
                continue
            backend.register_disconnect_callback(self._on_disconnect)
            return backend
        raise last_error  # type: ignore[misc]

    async def _connect_ftms(self) -> None:
        """Connect using the FTMS protocol with retry logic."""
        self._ftms = await self._connect_with_retries(
            "FTMS",
            FTMSController,
            lambda ftms: ftms.connect(self._ble_device, self._name),
        )

    async def _connect_wilink(self) -> None:
        """Connect using the legacy WiLink protocol (no retries)."""
        from .wilink import WiLinkController

        self._wilink = WiLinkController()
        self._wilink.register_status_callback(self._on_status_update)
        self._wilink.register_disconnect_callback(self._on_disconnect)
        await self._wilink.connect(self._ble_device)

    async def _connect_sperax(self) -> None:
        """Connect using the Sperax / WLT6200 protocol with retry logic."""
        from .sperax import SperaxController

        self._sperax = await self._connect_with_retries(
            "Sperax",
            SperaxController,
            lambda sperax: sperax.connect(self._ble_device),
        )

    async def disconnect(self) -> None:
        """Disconnect from the device.

        Tears down the backend whenever either the cached ``_connected`` flag
        or the backend itself says the link is up. Relying on the flag alone
        misses the case where the firmware dropped the link (so our callback
        cleared the flag) but Bleak still holds an open client to close.
        """
        async with self._lock:
            backend = self._backend
            backend_alive = backend is not None and backend.connected
            if not self._connected and not backend_alive:
                return
            try:
                if backend is not None:
                    await backend.disconnect()
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

        For WiLink devices, sends the standard start command. Sperax resumes
        at the cached target speed (at least the minimum).

        Returns:
            True if the belt is running (FTMS) or the command was sent.
            False on a lost connection, timeout, rejection or a pause
            during spin-up.
        """
        backend = self._backend
        if backend is not None:
            return await backend.start()

        _LOGGER.warning("No protocol backend available")
        return False

    async def stop(self) -> bool:
        """Stop the treadmill — full session end. Counters reset.

        On FTMS this sends `STOP_OR_PAUSE` with the STOP parameter (0x01).
        On devices that reject STOP while the belt moves (KingSmith R3,
        `KS-AP-*`) it pauses first and sends STOP only once fresh telemetry
        confirms 0 km/h.
        For most user-facing UIs you probably want `pause()` instead — that
        matches the phone-app and physical-remote behaviour where pressing
        the stop button leaves the session live and resumable.

        Returns:
            True if the command was sent successfully.
        """
        backend = self._backend
        if backend is not None:
            return await backend.stop()

        _LOGGER.warning("No protocol backend available")
        return False

    async def pause(self) -> bool:
        """Pause the treadmill — session stays live, can be resumed.

        On FTMS this sends `STOP_OR_PAUSE` with the PAUSE parameter (0x02).
        The device decelerates to zero but preserves session counters
        (time, distance, calories, steps) so a subsequent `start()` resumes
        from where the user left off — same behaviour as pressing the
        physical stop button or the stop button in KS Fit.

        On the legacy WiLink protocol there is no separate pause opcode;
        we fall back to stop() and log a warning.

        Returns:
            True if the command was sent successfully.
        """
        if self._ftms:
            return await self._ftms.pause()
        elif self._wilink:
            _LOGGER.warning(
                "WiLink protocol has no separate pause; falling back to stop"
            )
            return await self._wilink.stop()
        elif self._sperax:
            # Sperax has its own pause state (counters kept).
            return await self._sperax.pause()

        _LOGGER.warning("No protocol backend available")
        return False

    async def set_speed(self, speed_kmh: float) -> bool:
        """Set the treadmill speed.

        A speed of 0 or less always asks the belt to pause and never starts
        it. Otherwise, per protocol: FTMS sends SET_TARGET_SPEED directly if
        the belt runs, else starts it, waits for motion, then sets the speed;
        WiLink and Sperax send the speed directly.

        Args:
            speed_kmh: Target speed in km/h.

        Returns:
            True if the command was sent successfully.
        """
        if speed_kmh <= 0:
            # 0 km/h means "halt", not "minimum speed" — and must never
            # start a stopped belt.
            # The cached speed can be stale (e.g. 0 right after a reconnect),
            # so always ask the belt to pause; a stopped belt may reject it.
            # A disconnect resets the cached speed to 0, which proves nothing
            # about the belt, so only trust it while still connected.
            paused = await self.pause()
            return paused or (self.connected and self.status.speed == 0)

        if self._ftms:
            # The FTMS backend decides between "set directly" and "start
            # first" under its operation lock (cold-start and run-down
            # invariants, see FTMSController.set_speed).
            return await self._ftms.set_speed(speed_kmh)

        elif self._wilink:
            return await self._wilink.set_target_speed(speed_kmh)

        elif self._sperax:
            # The WLT6200 run command carries the speed directly and there is
            # no cold-start crash to work around, so a single call suffices
            # whether the belt is stopped or already moving.
            return await self._sperax.set_target_speed(speed_kmh)

        _LOGGER.warning("No protocol backend available")
        return False

    async def set_incline(self, incline_step: int) -> bool:
        """Set the incline as a discrete step.

        Only the Sperax / WLT6200 backend supports incline today (steps 0-10).
        FTMS/WiLink backends return False.

        Args:
            incline_step: Incline step (0-10).

        Returns:
            True if the command was sent successfully.
        """
        if self._sperax:
            return await self._sperax.set_target_inclination(incline_step)

        _LOGGER.warning("Incline control not supported on this device")
        return False

    async def set_vibration(self, level: int) -> bool:
        """Set the vibration level (0 = off, 1-4).

        Only the Sperax / WLT6200 backend supports vibration. Note the belt
        and vibration motor are mutually exclusive on the P3 Max. FTMS/WiLink
        backends return False.

        Args:
            level: Vibration level (0 = off, 1-4).

        Returns:
            True if the command was sent successfully.
        """
        if self._sperax:
            return await self._sperax.set_vibration(level)

        _LOGGER.warning("Vibration control not supported on this device")
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

        elif self._sperax:
            return await self._sperax.switch_mode(mode.value)

        _LOGGER.warning("No protocol backend available")
        return False

    async def update_state(self) -> None:
        """Request current state from the device.

        For FTMS and Sperax devices, status is pushed (notifications / the
        poll loop), so this fires a synthetic update from cached data. For
        WiLink devices, this polls the device.
        """
        if self._wilink:
            await self._wilink.ask_stats()
            return
        backend = self._backend
        if backend is None:
            return
        if backend.connected:
            self._on_status_update(backend.status)
        else:
            self._connected = False

    def set_name(self, name: str) -> None:
        """Set the name used when the BLEDevice carries none.

        It is the fallback for name-based protocol detection on the next
        ``connect()`` (e.g. a name Home Assistant learned from a scan
        response after setup). The live ``BLEDevice.name`` still wins.
        """
        if name:
            self._name = name

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLE device reference (e.g., after rediscovery).

        Args:
            ble_device: The new BLE device reference.
        """
        self._ble_device = ble_device
