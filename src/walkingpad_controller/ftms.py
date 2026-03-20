"""FTMS (Fitness Machine Service) protocol implementation for KingSmith treadmills.

This module implements the standard Bluetooth FTMS protocol (service 0x1826)
for controlling KingSmith treadmills that use the newer BLE chip.

These devices expose:
  - FTMS Service (0x1826) with Control Point (0x2AD9) for commands
  - Treadmill Data (0x2ACD) for real-time status via notifications
  - Custom supplement service (24e2521c-...) for extended features

Basic control (start/stop/speed) uses standard FTMS Control Point only.

BLE Connection Stability Note:
  KingSmith FTMS devices (e.g., KS-Z1D) at weak signal (~-77 dBm RSSI)
  may experience frequent BLE disconnects. The implementation includes
  retry logic and cold-start handling to deal with this.

Protocol reference:
  - Bluetooth SIG FTMS specification (Fitness Machine Service)
  - Reverse-engineered from KS Fit app v6.0.7 (ks_blue Dart package)
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .const import (
    FITNESS_MACHINE_STATUS_UUID,
    FTMS_CONTROL_POINT_UUID,
    FTMS_FEATURE_UUID,
    SUPPLEMENT_SERVICE_UUID,
    SUPPORTED_SPEED_RANGE_UUID,
    TREADMILL_DATA_UUID,
    FTMSOpcode,
    FTMSResultCode,
    FTMSStopPauseParam,
    TreadmillDataFlags,
)
from .models import DeviceCapabilities, SpeedRange, TreadmillStatus

_LOGGER = logging.getLogger(__name__)


class FTMSController:
    """Controller for KingSmith FTMS treadmills.

    Handles BLE connection, FTMS Control Point commands, and
    Treadmill Data notification parsing.
    """

    def __init__(self) -> None:
        self._client: BleakClient | None = None
        self._connected = False
        self._has_control = False
        self._status = TreadmillStatus()
        self._capabilities = DeviceCapabilities()
        self._status_callbacks: list[Callable[[TreadmillStatus], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []

        # Control point indication response
        self._cp_response_event = asyncio.Event()
        self._cp_response_data: bytes = b""

    @property
    def connected(self) -> bool:
        """Return whether the device is connected."""
        return (
            self._connected and self._client is not None and self._client.is_connected
        )

    @property
    def status(self) -> TreadmillStatus:
        """Return the current treadmill status."""
        return self._status

    @property
    def capabilities(self) -> DeviceCapabilities:
        """Return the device capabilities."""
        return self._capabilities

    @property
    def min_speed(self) -> float:
        """Minimum speed in km/h."""
        return self._capabilities.speed_range.min_speed

    @property
    def max_speed(self) -> float:
        """Maximum speed in km/h."""
        return self._capabilities.speed_range.max_speed

    @property
    def speed_increment(self) -> float:
        """Speed increment in km/h."""
        return self._capabilities.speed_range.increment

    def register_status_callback(
        self, callback: Callable[[TreadmillStatus], None]
    ) -> None:
        """Register a callback for status updates."""
        self._status_callbacks.append(callback)

    def register_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for disconnect events."""
        self._disconnect_callbacks.append(callback)

    def _notify_status(self) -> None:
        """Notify all registered callbacks of a status update."""
        for cb in self._status_callbacks:
            try:
                cb(self._status)
            except Exception:
                _LOGGER.exception("Error in status callback")

    # --- Connection ---

    async def connect(self, ble_device: BLEDevice) -> None:
        """Connect to the FTMS treadmill.

        Args:
            ble_device: The BLE device to connect to.
        """
        _LOGGER.info("FTMS: Connecting to %s", ble_device.address)

        self._client = BleakClient(
            ble_device, disconnected_callback=self._on_disconnect
        )
        await self._client.connect()
        self._connected = True

        _LOGGER.info("FTMS: Connected to %s", ble_device.address)

        # Discover services and log them
        await self._discover_services()

        # Read device capabilities
        await self._read_capabilities()

        # Subscribe to notifications
        await self._subscribe_notifications()

        # Request control
        await self._request_control()

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
        self._connected = False
        self._has_control = False

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle disconnection."""
        _LOGGER.warning("FTMS: Device disconnected")
        self._connected = False
        self._has_control = False
        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in disconnect callback")

    async def _discover_services(self) -> None:
        """Discover and log BLE services."""
        if not self._client:
            return

        for service in self._client.services:
            _LOGGER.debug("FTMS: [Service] %s", service.uuid)
            for char in service.characteristics:
                _LOGGER.debug(
                    "FTMS:   [Char] %s (Handle: %d) (%s)",
                    char.uuid,
                    char.handle,
                    ",".join(char.properties),
                )

        # Check for supplement service
        try:
            supplement_service = self._client.services.get_service(
                SUPPLEMENT_SERVICE_UUID
            )
            if supplement_service:
                self._capabilities.has_supplement = True
                _LOGGER.info("FTMS: Supplement service detected")
        except Exception:
            self._capabilities.has_supplement = False

    async def _read_capabilities(self) -> None:
        """Read device capabilities from FTMS characteristics."""
        if not self._client:
            return

        # Read Supported Speed Range (2AD4)
        try:
            speed_data = await self._client.read_gatt_char(SUPPORTED_SPEED_RANGE_UUID)
            if len(speed_data) >= 6:
                min_speed_raw = struct.unpack_from("<H", speed_data, 0)[0]
                max_speed_raw = struct.unpack_from("<H", speed_data, 2)[0]
                increment_raw = struct.unpack_from("<H", speed_data, 4)[0]
                self._capabilities.speed_range = SpeedRange(
                    min_speed=min_speed_raw / 100.0,
                    max_speed=max_speed_raw / 100.0,
                    increment=increment_raw / 100.0,
                )
                _LOGGER.info(
                    "FTMS: Speed range: %.2f - %.2f km/h (step %.2f)",
                    self._capabilities.speed_range.min_speed,
                    self._capabilities.speed_range.max_speed,
                    self._capabilities.speed_range.increment,
                )
        except Exception as err:
            _LOGGER.warning("FTMS: Failed to read speed range: %s", err)

        # Read Fitness Machine Feature (2ACC)
        try:
            feature_data = await self._client.read_gatt_char(FTMS_FEATURE_UUID)
            if len(feature_data) >= 8:
                self._capabilities.machine_features = struct.unpack_from(
                    "<I", feature_data, 0
                )[0]
                self._capabilities.target_features = struct.unpack_from(
                    "<I", feature_data, 4
                )[0]
                _LOGGER.info(
                    "FTMS: Machine features: 0x%08x, Target features: 0x%08x",
                    self._capabilities.machine_features,
                    self._capabilities.target_features,
                )
        except Exception as err:
            _LOGGER.warning("FTMS: Failed to read features: %s", err)

    async def _subscribe_notifications(self) -> None:
        """Subscribe to FTMS data notifications."""
        if not self._client:
            return

        # Subscribe to Treadmill Data (2ACD)
        try:
            await self._client.start_notify(
                TREADMILL_DATA_UUID, self._on_treadmill_data
            )
            _LOGGER.debug("FTMS: Subscribed to Treadmill Data")
        except Exception as err:
            _LOGGER.warning("FTMS: Failed to subscribe to Treadmill Data: %s", err)

        # Subscribe to Fitness Machine Status (2ADA)
        try:
            await self._client.start_notify(
                FITNESS_MACHINE_STATUS_UUID, self._on_machine_status
            )
            _LOGGER.debug("FTMS: Subscribed to Fitness Machine Status")
        except Exception as err:
            _LOGGER.warning("FTMS: Failed to subscribe to Machine Status: %s", err)

        # Subscribe to Control Point indications (2AD9)
        try:
            await self._client.start_notify(
                FTMS_CONTROL_POINT_UUID, self._on_control_point_response
            )
            _LOGGER.debug("FTMS: Subscribed to Control Point indications")
        except Exception as err:
            _LOGGER.warning("FTMS: Failed to subscribe to Control Point: %s", err)

    # --- Notification Handlers ---

    def _on_treadmill_data(self, sender: int, data: bytearray) -> None:
        """Handle Treadmill Data (2ACD) notifications.

        Parses the standard FTMS Treadmill Data characteristic per the
        Bluetooth SIG Fitness Machine Service specification.

        KingSmith extensions:
        - Bit 13 (0x2000): 3 extra bytes — uint16 LE step count + 1 zero byte.
          The step counter is pressure-sensor based (only counts when walking).
        """
        if len(data) < 4:
            return

        offset = 0
        flags = struct.unpack_from("<H", data, offset)[0]
        offset += 2

        # Instantaneous Speed - always present (UINT16, 0.01 km/h)
        speed_raw = struct.unpack_from("<H", data, offset)[0]
        self._status.speed = speed_raw / 100.0
        self._status.belt_state = 1 if speed_raw > 0 else 0
        offset += 2

        # Average Speed (bit 1)
        if flags & TreadmillDataFlags.AVERAGE_SPEED:
            if offset + 2 <= len(data):
                offset += 2

        # Total Distance (bit 2) - UINT24 in meters
        if flags & TreadmillDataFlags.TOTAL_DISTANCE:
            if offset + 3 <= len(data):
                dist_bytes = data[offset : offset + 3]
                self._status.distance = (
                    dist_bytes[0] | (dist_bytes[1] << 8) | (dist_bytes[2] << 16)
                )
                offset += 3

        # Inclination and Ramp Angle (bit 3) - INT16 + INT16
        if flags & TreadmillDataFlags.INCLINATION:
            if offset + 4 <= len(data):
                offset += 4

        # Elevation Gain (bit 4) - UINT16 + UINT16
        if flags & TreadmillDataFlags.ELEVATION_GAIN:
            offset += 4

        # Instantaneous Pace (bit 5) - UINT8
        if flags & TreadmillDataFlags.INSTANTANEOUS_PACE:
            offset += 1

        # Average Pace (bit 6) - UINT8
        if flags & TreadmillDataFlags.AVERAGE_PACE:
            offset += 1

        # Expended Energy (bit 7) - UINT16 + UINT16 + UINT8
        if flags & TreadmillDataFlags.EXPENDED_ENERGY:
            if offset + 5 <= len(data):
                self._status.calories = struct.unpack_from("<H", data, offset)[0]
                self._status.calories_per_hour = struct.unpack_from(
                    "<H", data, offset + 2
                )[0]
                offset += 5

        # Heart Rate (bit 8) - UINT8
        if flags & TreadmillDataFlags.HEART_RATE:
            if offset + 1 <= len(data):
                self._status.heart_rate = data[offset]
                offset += 1

        # Metabolic Equivalent (bit 9) - UINT8
        if flags & TreadmillDataFlags.METABOLIC_EQUIVALENT:
            offset += 1

        # Elapsed Time (bit 10) - UINT16 in seconds
        if flags & TreadmillDataFlags.ELAPSED_TIME:
            if offset + 2 <= len(data):
                self._status.duration = struct.unpack_from("<H", data, offset)[0]
                offset += 2

        # Remaining Time (bit 11) - UINT16
        if flags & TreadmillDataFlags.REMAINING_TIME:
            offset += 2

        # Force on Belt (bit 12) - INT16 + INT16
        if flags & TreadmillDataFlags.FORCE_ON_BELT:
            offset += 4

        # KingSmith Extension (bit 13) - 3 bytes: uint16 LE step count + 1 zero byte
        if flags & TreadmillDataFlags.KINGSMITH_EXTENSION:
            if offset + 3 <= len(data):
                self._status.steps = struct.unpack_from("<H", data, offset)[0]
                offset += 3

        self._status.mode = 1  # FTMS is always manual mode
        self._status.timestamp = time.time()

        _LOGGER.debug(
            "FTMS: speed=%.2f km/h, dist=%dm, cal=%d, time=%ds, steps=%d",
            self._status.speed,
            self._status.distance,
            self._status.calories,
            self._status.duration,
            self._status.steps,
        )

        self._notify_status()

    def _on_machine_status(self, sender: int, data: bytearray) -> None:
        """Handle Fitness Machine Status (2ADA) notifications."""
        if len(data) < 1:
            return

        opcode = data[0]
        _LOGGER.debug(
            "FTMS: Machine status event: 0x%02x (data: %s)", opcode, data.hex()
        )

        if opcode == 0x02:  # Stopped or paused
            if len(data) >= 2:
                if data[1] == 0x01:
                    _LOGGER.info("FTMS: Treadmill stopped by user")
                elif data[1] == 0x02:
                    _LOGGER.info("FTMS: Treadmill paused by user")
        elif opcode == 0x03:
            _LOGGER.info("FTMS: Treadmill stopped by safety key")
        elif opcode == 0x04:
            _LOGGER.info("FTMS: Treadmill started/resumed by user")

    def _on_control_point_response(self, sender: int, data: bytearray) -> None:
        """Handle FTMS Control Point (2AD9) indication responses.

        Response format: [0x80, request_opcode, result_code, ...]
        """
        _LOGGER.debug("FTMS: Control Point response: %s", data.hex())
        self._cp_response_data = bytes(data)
        self._cp_response_event.set()

    # --- Control Commands ---

    async def _write_control_point(
        self, opcode: FTMSOpcode, params: bytes = b"", timeout: float = 5.0
    ) -> bool:
        """Write a command to the FTMS Control Point and wait for response.

        Returns True if the command was acknowledged with success.
        """
        if not self._client or not self._client.is_connected:
            _LOGGER.warning("FTMS: Not connected, cannot send command")
            return False

        command = bytes([opcode]) + params
        _LOGGER.debug("FTMS: Sending control point command: %s", command.hex())

        self._cp_response_event.clear()

        try:
            await self._client.write_gatt_char(
                FTMS_CONTROL_POINT_UUID, command, response=True
            )
        except BleakError as err:
            _LOGGER.warning("FTMS: Write error: %s", err)
            return False

        # Wait for indication response
        try:
            await asyncio.wait_for(self._cp_response_event.wait(), timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "FTMS: Control point response timeout for opcode 0x%02x", opcode
            )
            return False

        # Parse response: [0x80, request_opcode, result_code]
        resp = self._cp_response_data
        if len(resp) >= 3 and resp[0] == FTMSOpcode.RESPONSE_CODE:
            req_opcode = resp[1]
            result = resp[2]
            if req_opcode == opcode and result == FTMSResultCode.SUCCESS:
                _LOGGER.debug("FTMS: Command 0x%02x succeeded", opcode)
                return True
            else:
                _LOGGER.warning(
                    "FTMS: Command 0x%02x result: %d (request_opcode: 0x%02x)",
                    opcode,
                    result,
                    req_opcode,
                )
                return False

        _LOGGER.warning("FTMS: Unexpected control point response: %s", resp.hex())
        return False

    async def _request_control(self) -> bool:
        """Request control of the fitness machine."""
        result = await self._write_control_point(FTMSOpcode.REQUEST_CONTROL)
        if result:
            self._has_control = True
            _LOGGER.info("FTMS: Control acquired")
        else:
            _LOGGER.warning("FTMS: Failed to acquire control")
        return result

    async def start(self) -> bool:
        """Start or resume the treadmill belt.

        Sends START_OR_RESUME and, on a cold start, waits for the belt to
        report speed > 0.  Does NOT send SET_TARGET_SPEED — the user sets
        the speed explicitly via set_target_speed() (e.g. the HA speed
        slider).  Sending a speed command during motor spin-up crashes the
        BLE connection on KingSmith firmware.

        Returns:
            True if the belt is running. False if the connection was lost.
        """
        if not self._has_control:
            await self._request_control()

        cold_start = await self._write_control_point(FTMSOpcode.START_OR_RESUME)
        if cold_start:
            _LOGGER.info("FTMS: START_OR_RESUME succeeded (cold start)")
        else:
            _LOGGER.debug("FTMS: START_OR_RESUME not needed (belt already running)")

        if not self.connected:
            _LOGGER.warning("FTMS: Connection lost after START_OR_RESUME")
            return False

        if cold_start:
            belt_running = await self._wait_for_belt_moving(timeout=15.0)
            if not belt_running:
                if not self.connected:
                    _LOGGER.warning("FTMS: Connection lost waiting for belt to start")
                    return False
                _LOGGER.warning("FTMS: Belt did not start moving within timeout")
                return False
            _LOGGER.info(
                "FTMS: Cold start complete — belt running at %.1f km/h",
                self._status.speed,
            )

        return True

    async def _wait_for_belt_moving(self, timeout: float = 15.0) -> bool:
        """Wait for the belt to report speed > 0 after a cold start.

        Polls treadmill-data notifications until the belt is physically
        moving.  No stabilisation delay is applied here — we deliberately
        avoid sending any speed command while the connection is fragile.

        Args:
            timeout: Maximum time to wait for speed > 0 (seconds).

        Returns True if the belt is moving, False on timeout/disconnect.
        """
        _LOGGER.debug(
            "FTMS: Waiting for belt to start moving (timeout=%.0fs)...", timeout
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.connected:
                return False
            if self._status.speed > 0:
                wait_elapsed = timeout - (deadline - time.time())
                _LOGGER.info(
                    "FTMS: Belt moving at %.1f km/h (waited %.1fs)",
                    self._status.speed,
                    wait_elapsed,
                )
                return True
            await asyncio.sleep(0.5)
        return False

    async def stop(self) -> bool:
        """Stop the treadmill."""
        if not self._has_control:
            await self._request_control()
        return await self._write_control_point(
            FTMSOpcode.STOP_OR_PAUSE,
            bytes([FTMSStopPauseParam.STOP]),
        )

    async def pause(self) -> bool:
        """Pause the treadmill."""
        if not self._has_control:
            await self._request_control()
        return await self._write_control_point(
            FTMSOpcode.STOP_OR_PAUSE,
            bytes([FTMSStopPauseParam.PAUSE]),
        )

    async def reset(self) -> bool:
        """Reset the fitness machine."""
        if not self._has_control:
            await self._request_control()
        return await self._write_control_point(FTMSOpcode.RESET)

    async def set_target_speed(self, speed_kmh: float) -> bool:
        """Set the target speed in km/h.

        The speed is clamped to the device's supported range and rounded
        to the nearest supported increment.

        Args:
            speed_kmh: Target speed in km/h (e.g., 3.5)

        Returns:
            True if the command was acknowledged with success.
        """
        sr = self._capabilities.speed_range

        # Clamp to supported range
        speed_kmh = max(sr.min_speed, min(sr.max_speed, speed_kmh))

        # Round to nearest increment
        if sr.increment > 0:
            steps = round(speed_kmh / sr.increment)
            speed_kmh = steps * sr.increment

        # Convert to UINT16 in 0.01 km/h units
        speed_raw = int(round(speed_kmh * 100))
        params = struct.pack("<H", speed_raw)

        if not self._has_control:
            await self._request_control()

        _LOGGER.debug(
            "FTMS: Setting target speed to %.2f km/h (raw: %d)", speed_kmh, speed_raw
        )
        return await self._write_control_point(FTMSOpcode.SET_TARGET_SPEED, params)

    async def set_target_inclination(self, inclination_pct: float) -> bool:
        """Set the target inclination in percent.

        Args:
            inclination_pct: Target inclination in percent (e.g., 5.0 for 5%)

        Returns:
            True if the command was acknowledged with success.
        """
        inclination_raw = int(round(inclination_pct * 10))
        params = struct.pack("<h", inclination_raw)

        if not self._has_control:
            await self._request_control()

        return await self._write_control_point(
            FTMSOpcode.SET_TARGET_INCLINATION, params
        )
