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
    KINGSMITH_VENDOR_PREAMBLE_PAYLOAD,
    KINGSMITH_VENDOR_PREAMBLE_UUID,
    SOFTWARE_REVISION_UUID,
    SUPPLEMENT_SERVICE_UUID,
    SUPPORTED_SPEED_RANGE_UUID,
    TRAINING_STATUS_UUID,
    TREADMILL_DATA_UUID,
    FitnessMachineStatusOpcode,
    FTMSOpcode,
    FTMSResultCode,
    FTMSStopPauseParam,
    TreadmillDataFlags,
)
from .models import DeviceCapabilities, SpeedRange, TreadmillStatus

_LOGGER = logging.getLogger(__name__)


# Mapping from FTMS Control Point opcode to the Fitness Machine Status (2ADA)
# event that confirms the command was applied. Used on the pre-amble path,
# where the firmware acks most commands via 2ADA rather than a CP indication.
_CP_TO_STATUS_ACK: dict[int, int] = {
    FTMSOpcode.START_OR_RESUME: FitnessMachineStatusOpcode.STARTED_OR_RESUMED,
    FTMSOpcode.STOP_OR_PAUSE: FitnessMachineStatusOpcode.STOPPED_OR_PAUSED,
    FTMSOpcode.SET_TARGET_SPEED: FitnessMachineStatusOpcode.TARGET_SPEED_CHANGED,
    FTMSOpcode.SET_TARGET_INCLINATION: FitnessMachineStatusOpcode.TARGET_INCLINATION_CHANGED,
}


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

        # Fitness Machine Status (2ADA) ack — used on the vendor pre-amble
        # path, where most Control Point opcodes are acknowledged via a
        # status event rather than a CP indication.
        self._status_ack_event = asyncio.Event()
        self._status_ack_expected_opcode: int | None = None

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

    @property
    def firmware_version(self) -> str:
        """Firmware version string read from the device, or empty if unavailable."""
        return self._capabilities.firmware_version

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

        Raises:
            BleakError: If the underlying BLE link drops before setup
                finishes (e.g. shortly after a previous disconnect, the
                firmware sometimes accepts the connection and then closes
                it again before service discovery completes).
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

        # Sanity check: if the link dropped at any point during setup
        # (Bleak's is_connected goes False, our _connected bit is flipped
        # by the disconnect callback), surface that as a failed connect
        # rather than silently claiming success — otherwise callers see
        # `connected == False` immediately after `connect()` "succeeds"
        # and have no clean signal that they should retry.
        if not self.connected:
            raise BleakError(
                "FTMS: BLE link dropped during connection setup; treating "
                "as a failed connect."
            )

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

        # Check for the KingSmith MC-21 vendor pre-amble characteristic.
        # If present, we'll write the magic payload before every Control
        # Point command — that's what KS Fit does, and without it the
        # firmware refuses SET_TARGET_SPEED.
        try:
            char = self._client.services.get_characteristic(
                KINGSMITH_VENDOR_PREAMBLE_UUID
            )
            if char is not None:
                self._capabilities.has_vendor_preamble = True
                _LOGGER.info("FTMS: KingSmith vendor pre-amble characteristic detected")
        except Exception:
            self._capabilities.has_vendor_preamble = False

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

        # Read Software Revision String (2A28). Best-effort: some devices
        # don't expose it. KS Fit reads this for the firmware-version display.
        try:
            sw_data = await self._client.read_gatt_char(SOFTWARE_REVISION_UUID)
            if sw_data:
                self._capabilities.firmware_version = sw_data.decode(
                    "utf-8", errors="replace"
                ).strip("\x00 \t\r\n")
                _LOGGER.info(
                    "FTMS: Firmware version: %s",
                    self._capabilities.firmware_version,
                )
        except Exception as err:
            _LOGGER.debug("FTMS: Failed to read firmware version: %s", err)

    async def _subscribe_notifications(self) -> None:
        """Subscribe to FTMS data notifications.

        KingSmith firmware silently drops CCCD writes that arrive in close
        succession. KS Fit staggers its subscriptions with progressive delays
        of 100/200/300 ms — we mirror that. See `docs/ftms-protocol-reference.md`
        §2.1 for the analysis behind these numbers.
        """
        if not self._client:
            return

        subscriptions = (
            (TREADMILL_DATA_UUID, self._on_treadmill_data, "Treadmill Data", 0.10),
            (FITNESS_MACHINE_STATUS_UUID, self._on_machine_status, "Fitness Machine Status", 0.20),
            (TRAINING_STATUS_UUID, self._on_training_status, "Training Status", 0.30),
            (FTMS_CONTROL_POINT_UUID, self._on_control_point_response, "Control Point", 0.0),
        )

        for uuid, handler, label, delay_after in subscriptions:
            try:
                await self._client.start_notify(uuid, handler)
                _LOGGER.debug("FTMS: Subscribed to %s", label)
            except Exception as err:
                _LOGGER.warning("FTMS: Failed to subscribe to %s: %s", label, err)
            if delay_after:
                await asyncio.sleep(delay_after)

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
        """Handle Fitness Machine Status (2ADA) notifications.

        On KingSmith firmware that uses the vendor pre-amble (e.g. MC-21),
        these events are how command success is signalled — the device
        sends *no* indication on the Control Point itself for opcodes other
        than REQUEST_CONTROL.
        """
        if len(data) < 1:
            return

        opcode = data[0]
        _LOGGER.debug(
            "FTMS: Machine status event: 0x%02x (data: %s)", opcode, data.hex()
        )

        # Wake any pending command waiter that's expecting this opcode.
        if (
            self._status_ack_expected_opcode is not None
            and opcode == self._status_ack_expected_opcode
        ):
            self._status_ack_event.set()

        if opcode == FitnessMachineStatusOpcode.STOPPED_OR_PAUSED:
            if len(data) >= 2:
                if data[1] == FTMSStopPauseParam.STOP:
                    _LOGGER.info("FTMS: Treadmill stopped by user")
                elif data[1] == FTMSStopPauseParam.PAUSE:
                    _LOGGER.info("FTMS: Treadmill paused by user")
        elif opcode == FitnessMachineStatusOpcode.STOPPED_BY_SAFETY_KEY:
            _LOGGER.info("FTMS: Treadmill stopped by safety key")
        elif opcode == FitnessMachineStatusOpcode.STARTED_OR_RESUMED:
            _LOGGER.info("FTMS: Treadmill started/resumed by user")
        elif opcode == FitnessMachineStatusOpcode.TARGET_SPEED_CHANGED:
            if len(data) >= 3:
                speed_raw = struct.unpack_from("<H", data, 1)[0]
                _LOGGER.info(
                    "FTMS: Target speed changed to %.2f km/h", speed_raw / 100.0
                )
        elif opcode == FitnessMachineStatusOpcode.TARGET_INCLINATION_CHANGED:
            if len(data) >= 3:
                incl_raw = struct.unpack_from("<h", data, 1)[0]
                _LOGGER.info(
                    "FTMS: Target inclination changed to %.1f%%", incl_raw / 10.0
                )

    def _on_training_status(self, sender: int, data: bytearray) -> None:
        """Handle Training Status (2AD3) notifications.

        Standard FTMS Training Status frame:
          byte 0: flags (bit 0 = string present, bit 1 = extended info)
          byte 1: training status code (0=other, 1=idle, 2=warming up, …)
          bytes 2+: optional UTF-8 label / extended data (when bit 0 set)
        """
        if len(data) < 2:
            return
        flags = data[0]
        status_code = data[1]
        _LOGGER.debug(
            "FTMS: Training status flags=0x%02x code=0x%02x raw=%s",
            flags,
            status_code,
            data.hex(),
        )

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

        if self._capabilities.has_vendor_preamble:
            try:
                await self._client.write_gatt_char(
                    KINGSMITH_VENDOR_PREAMBLE_UUID,
                    KINGSMITH_VENDOR_PREAMBLE_PAYLOAD,
                    response=True,
                )
            except BleakError as err:
                _LOGGER.debug("FTMS: Vendor pre-amble write error: %s", err)

        # On the pre-amble path, the device acknowledges most opcodes via a
        # Fitness Machine Status (2ADA) event rather than a Control Point
        # indication. Set up an expectation now so the status handler can
        # signal us when the matching event arrives.
        expected_status_opcode = _CP_TO_STATUS_ACK.get(opcode)
        if (
            self._capabilities.has_vendor_preamble
            and expected_status_opcode is not None
        ):
            self._status_ack_expected_opcode = expected_status_opcode
            self._status_ack_event.clear()

        command = bytes([opcode]) + params
        _LOGGER.debug("FTMS: Sending control point command: %s", command.hex())

        self._cp_response_event.clear()

        try:
            await self._client.write_gatt_char(
                FTMS_CONTROL_POINT_UUID, command, response=True
            )
        except BleakError as err:
            self._status_ack_expected_opcode = None
            _LOGGER.warning("FTMS: Write error: %s", err)
            return False

        # Wait for either a Control Point indication OR a matching Fitness
        # Machine Status event. On standard FTMS firmware the indication
        # arrives quickly (sub-second). On the pre-amble path most opcodes
        # only get a 2ADA event; first-call REQUEST_CONTROL still gets an
        # indication. Race them so whichever arrives first wins.
        effective_timeout = (
            3.0 if self._capabilities.has_vendor_preamble else timeout
        )
        waiters = [asyncio.create_task(self._cp_response_event.wait())]
        if self._capabilities.has_vendor_preamble and expected_status_opcode is not None:
            waiters.append(asyncio.create_task(self._status_ack_event.wait()))

        try:
            done, pending = await asyncio.wait(
                waiters,
                timeout=effective_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for w in waiters:
                if not w.done():
                    w.cancel()

        # Always clear the expectation, regardless of outcome.
        self._status_ack_expected_opcode = None

        # Status event won the race → command was acknowledged by the device.
        if self._status_ack_event.is_set():
            _LOGGER.debug(
                "FTMS: Status-event ack for opcode 0x%02x", opcode
            )
            return True

        # Indication won the race → fall through to the standard parser below.
        if not self._cp_response_event.is_set():
            # Neither arrived in time.
            if self._capabilities.has_vendor_preamble:
                _LOGGER.debug(
                    "FTMS: No ack for opcode 0x%02x — assuming success "
                    "(vendor pre-amble path)",
                    opcode,
                )
                return True
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
        """Request control of the fitness machine.

        Some KingSmith firmware (e.g. MC-21) rejects REQUEST_CONTROL with
        OPERATION_FAILED / CONTROL_NOT_PERMITTED but still accepts the
        commands that follow. KS Fit ignores the failure and proceeds; we
        do the same — `_has_control` is set regardless of the response so
        every subsequent command doesn't keep retrying a call we know will
        fail. See issue #1.
        """
        result = await self._write_control_point(FTMSOpcode.REQUEST_CONTROL)
        self._has_control = True
        if result:
            _LOGGER.info("FTMS: Control acquired")
        else:
            _LOGGER.warning(
                "FTMS: REQUEST_CONTROL was rejected — proceeding anyway "
                "(some KingSmith firmware refuses this command but still "
                "accepts START_OR_RESUME / STOP_OR_PAUSE)"
            )
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

        if not self.connected:
            _LOGGER.warning("FTMS: Not connected; cannot start")
            return False

        accepted = await self._write_control_point(FTMSOpcode.START_OR_RESUME)

        if not self.connected:
            _LOGGER.warning("FTMS: Connection lost during START_OR_RESUME")
            return False

        if accepted:
            _LOGGER.info("FTMS: START_OR_RESUME accepted (cold start)")
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

        # The device rejected START_OR_RESUME (non-success indication).
        # That can mean either (a) belt is already running, or (b) the
        # device is in a state that doesn't accept start right now —
        # e.g. just transitioned through stop and isn't fully settled.
        # Disambiguate by looking at the live speed.
        if self._status.speed > 0:
            _LOGGER.info(
                "FTMS: START_OR_RESUME rejected but belt is running at "
                "%.1f km/h — treating as success",
                self._status.speed,
            )
            return True

        _LOGGER.warning(
            "FTMS: START_OR_RESUME rejected and belt is not moving "
            "(device may need a moment after stop)"
        )
        return False

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
