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
  may experience frequent BLE disconnects. This module handles cold starts
  and setup failures; connection retries live in `WalkingPadController`.

Protocol reference:
  - Bluetooth SIG FTMS specification (Fitness Machine Service)
  - Reverse-engineered from KS Fit app v6.0.7 (ks_blue Dart package)
"""

from __future__ import annotations

import asyncio
import enum
import logging
import struct
import time
from typing import Callable, NamedTuple

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from ._callbacks import fire_callbacks
from .const import (
    FITNESS_MACHINE_STATUS_UUID,
    FTMS_CONTROL_POINT_UUID,
    FTMS_FEATURE_UUID,
    FTMS_PAUSE_BEFORE_STOP_PREFIXES,
    KINGSMITH_VENDOR_PREAMBLE_PAYLOAD,
    KINGSMITH_VENDOR_PREAMBLE_UUID,
    SOFTWARE_REVISION_UUID,
    SUPPLEMENT_SERVICE_UUID,
    SUPPORTED_SPEED_RANGE_UUID,
    TRAINING_STATUS_UUID,
    TREADMILL_DATA_UUID,
    BeltState,
    FitnessMachineStatusOpcode,
    FTMSOpcode,
    FTMSResultCode,
    FTMSStopPauseParam,
    TreadmillDataFlags,
)
from .models import DeviceCapabilities, SpeedRange, TreadmillStatus

_LOGGER = logging.getLogger(__name__)

# A first 0x2ADA event later than this after subscribing is a real event,
# not the firmware's replay of its previous state.
_STATUS_REPLAY_WINDOW = 2.0

# How long a Control Point write waits for its acknowledgement. On the vendor
# pre-amble path this replaces (not caps) the caller's timeout.
_CP_ACK_TIMEOUT = 5.0
_CP_ACK_TIMEOUT_PREAMBLE = 3.0

# How long start() waits for the belt to move after START_OR_RESUME.
_START_MOTION_TIMEOUT = 15.0


# Mapping from FTMS Control Point opcode to the Fitness Machine Status (2ADA)
# event that confirms the command was applied. Used on the pre-amble path,
# where the firmware acks most commands via 2ADA rather than a CP indication.
_CP_TO_STATUS_ACK: dict[int, int] = {
    FTMSOpcode.START_OR_RESUME: FitnessMachineStatusOpcode.STARTED_OR_RESUMED,
    FTMSOpcode.STOP_OR_PAUSE: FitnessMachineStatusOpcode.STOPPED_OR_PAUSED,
    FTMSOpcode.SET_TARGET_SPEED: FitnessMachineStatusOpcode.TARGET_SPEED_CHANGED,
    FTMSOpcode.SET_TARGET_INCLINATION: FitnessMachineStatusOpcode.TARGET_INCLINATION_CHANGED,
}


def _needs_pause_before_stop(name: str | None) -> bool:
    """Return whether a device with this BLE name rejects STOP while moving."""
    return (name or "").upper().startswith(FTMS_PAUSE_BEFORE_STOP_PREFIXES)


class _Subscription(NamedTuple):
    """One notification subscription made while connecting."""

    uuid: str
    handler: Callable[[int, bytearray], None]
    label: str
    delay_after: float  # seconds to wait before the next CCCD write
    required: bool  # a failure fails the connect


# Characteristics `_subscribe_notifications` subscribes to, in order.
_CONNECT_SUBSCRIPTION_UUIDS = (
    TREADMILL_DATA_UUID,
    FITNESS_MACHINE_STATUS_UUID,
    FTMS_CONTROL_POINT_UUID,
)


async def _wait_event(event: asyncio.Event, timeout: float) -> None:
    """Wait until ``event`` is set or ``timeout`` passes, whichever is first."""
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        pass


class _CPResult(enum.Enum):
    """How a Control Point write ended."""

    ACK = "ack"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    NOT_SENT = "not_sent"  # not connected, write error, or link lost


class _Standstill(enum.Enum):
    """Outcome of waiting for the belt to come to rest."""

    CONFIRMED = "confirmed"
    STILL_MOVING = "still_moving"
    NO_TELEMETRY = "no_telemetry"
    DISCONNECTED = "disconnected"


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
        # Opcode of the Control Point write in flight, if any.
        self._cp_pending_opcode: int | None = None

        # KingSmith firmware fires its current state on the 0x2ADA char as
        # soon as the CCCD is enabled — typically a stale STOPPED_OR_PAUSED
        # event from before HA connected. The first event after subscribing
        # is dropped if it looks like that replay (see
        # `_is_stale_status_replay`); this flag is set once it was examined.
        # See FTMS reverse-engineering doc, §6.3.
        self._machine_status_first_event_seen = False
        # When 0x2ADA was subscribed (monotonic). Only a first event that
        # arrives shortly after subscribing is treated as a stale replay.
        self._status_subscribed_at: float | None = None

        # Fitness Machine Status (2ADA) ack — used on the vendor pre-amble
        # path, where most Control Point opcodes are acknowledged via a
        # status event rather than a CP indication.
        self._status_ack_event = asyncio.Event()
        self._status_ack_expected_opcode: int | None = None
        self._status_ack_expected_param: int | None = None

        # True between a `STOPPED_OR_PAUSED + PAUSE` FM Status event and
        # the next `STARTED_OR_RESUMED` / `STOPPED_OR_PAUSED + STOP` /
        # safety-stop event. Used by `_on_treadmill_data` to derive
        # `belt_state = PAUSED` when speed reads 0 instead of the
        # default STOPPED — so HA can distinguish "session paused, will
        # resume" from "session ended, counters reset on next start".
        self._paused: bool = False

        # Serialises multi-step operations (start / stop / set speed /
        # inclination / reset) so e.g. a SET_TARGET_SPEED can't land between
        # stop()'s PAUSE and STOP. pause() deliberately does not take it: it
        # is the halt path and must never queue behind a multi-step
        # operation that is waiting on the belt.
        self._op_lock = asyncio.Lock()
        # Serialises Control Point write-and-wait, which shares a single
        # response slot (`_cp_response_event` / `_cp_response_data`).
        self._cp_lock = asyncio.Lock()

        # Treadmill Data bookkeeping for standstill confirmation: a counter
        # bumped on every frame plus an event to wake waiters. Speed in
        # `_status` can be stale (e.g. carried over from before a
        # reconnect); only frames counted after a command prove anything.
        self._treadmill_data_seq = 0
        self._treadmill_data_event = asyncio.Event()
        # Sequence number of the last frame with speed > 0, so a motion
        # waiter can't miss a moving frame that a 0 km/h frame overwrote
        # before it woke up.
        self._last_moving_seq = 0
        # Count of FM Status STOPPED_OR_PAUSED + STOP events (session end),
        # plus an event to wake a STOP waiting for it.
        self._session_end_events = 0
        self._session_end_event = asyncio.Event()
        # How long _send_stop() waits for the session-end event after the
        # STOP indication timed out.
        self._session_end_grace = 1.0
        # Bumped by pause() so a start() waiting for the belt gives up
        # instead of reporting success (and letting set_speed() re-accelerate).
        self._pause_seq = 0
        # How long stop() waits for the belt to come to rest after PAUSE.
        self._standstill_timeout = 15.0
        # How many PAUSE -> standstill -> STOP cycles stop() tries on
        # pause-before-stop devices before giving up.
        self._stop_attempts = 2
        # The R3 streams Treadmill Data at 1 Hz even when idle, but sometimes
        # ignores the CCCD writes right after connecting (no notifications at
        # all). How long connect waits for a first frame before resubscribing.
        self._telemetry_check_timeout = 2.5
        # After STOP is rejected at 0 km/h, how long stop() watches for the
        # belt to start (R3 start countdown is ~4 s; see stop()).
        self._countdown_grace = 6.0
        # After START_OR_RESUME is rejected, how long start() waits for a
        # fresh Treadmill Data frame to tell whether the belt is running.
        self._start_rejected_grace = 2.0

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
        fire_callbacks(
            self._status_callbacks, _LOGGER, "Error in status callback", self._status
        )

    # --- Connection ---

    async def connect(self, ble_device: BLEDevice, name: str | None = None) -> None:
        """Connect to the FTMS treadmill.

        Args:
            ble_device: The BLE device to connect to.
            name: Fallback device name for when ``ble_device.name`` is unset
                (e.g. the name stored in the Home Assistant config entry; a
                BT proxy may hand over a nameless BLEDevice after a restart).

        Raises:
            BleakError: If the underlying BLE link drops before setup
                finishes (e.g. shortly after a previous disconnect, the
                firmware sometimes accepts the connection and then closes
                it again before service discovery completes).
        """
        _LOGGER.info("FTMS: Connecting to %s", ble_device.address)

        # bleak_retry_connector wraps BleakClient.connect with HA-tuned retry
        # heuristics — slot management, scanner re-discovery, ESPHome BT-proxy
        # reliability fixes. Hand-rolled `BleakClient(...).connect()` works
        # but is fragile against `ESP_GATT_CONN_FAIL_ESTABLISH` / no-slot
        # conditions, which is exactly the failure mode KingSmith devices hit
        # at marginal RSSI through ESPHome proxies.
        self._client = await establish_connection(
            BleakClient,
            ble_device,
            ble_device.name or name or ble_device.address,
            disconnected_callback=self._on_disconnect,
        )
        self._connected = True

        _LOGGER.info("FTMS: Connected to %s", ble_device.address)
        try:
            await self._setup_connection(ble_device, name)
        except BaseException:
            # Don't leak a connected client (and its BLE slot) when setup
            # fails or is cancelled; the caller will retry with a new one.
            try:
                await self.disconnect()
            except Exception:
                _LOGGER.debug("FTMS: Error closing link after failed setup")
            raise

    async def _setup_connection(self, ble_device: BLEDevice, name: str | None) -> None:
        """Connection setup after the link is up; see `connect`."""

        # Events may still be set from waking waiters on a previous
        # connection of this instance.
        self._cp_response_event.clear()
        self._status_ack_event.clear()
        self._session_end_event.clear()
        self._capabilities.pause_before_stop = _needs_pause_before_stop(
            ble_device.name or name
        )
        if self._capabilities.pause_before_stop:
            _LOGGER.info(
                "FTMS: %s needs PAUSE before STOP", ble_device.name or name
            )

        # Discover services so we can detect supplement / vendor pre-amble
        # characteristics. Local (BlueZ-cached) — no extra GATT round-trips.
        await self._discover_services()

        # Read device capabilities (Speed Range, FTMS Feature, Software
        # Revision). Bisection in tests/probe_setup_killer.py showed pure
        # GATT reads aren't the destabilising operations; the suspect ones
        # are CCCD writes (subscribe) and the REQUEST_CONTROL Control-Point
        # write. Keep the reads — they give callers the real ranges.
        await self._read_capabilities()

        # Subscribe to the minimum needed for command-and-ack flow.
        # Training Status (0x2AD3) used to be subscribed here too but it's
        # informational only; dropping it cuts one CCCD write off the
        # connect critical path on marginal-signal hardware (e.g. KS-HD-Z1D
        # at -83 dBm RSSI).
        await self._subscribe_notifications()
        if self._capabilities.pause_before_stop:
            await self._ensure_telemetry()

        # Send REQUEST_CONTROL. We previously experimented with skipping
        # this (we ignore the response anyway), but on KS-HD-Z1D the
        # device silently rejects subsequent SET_TARGET_SPEED / STOP
        # writes if it never observed REQUEST_CONTROL — the firmware
        # tracks the request even though it doesn't always indicate.
        # Keep this on the connect critical path. `_has_control` is
        # set to True regardless of the response per issue #1.
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
        # Don't rely on Bleak firing the disconnect callback for an
        # explicit disconnect; reset state and wake waiters ourselves.
        self._reset_link_state()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except BleakError:
                pass

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle disconnection."""
        if client is not None and client is not self._client:
            return  # a retired client from an earlier connection
        _LOGGER.warning("FTMS: Device disconnected")
        self._reset_link_state()
        fire_callbacks(
            self._disconnect_callbacks, _LOGGER, "Error in disconnect callback"
        )

    def _reset_link_state(self) -> None:
        """Forget per-connection state after a disconnect.

        The belt state is unknown once the link is gone, so don't keep
        reporting the last speed as if the belt were still running.
        """
        changed = self._status.speed > 0 or self._status.belt_state != BeltState.STOPPED
        self._connected = False
        self._has_control = False
        self._paused = False
        # Re-arm the first-event skip so the next connection's stale
        # 2ADA replay is also dropped.
        self._machine_status_first_event_seen = False
        self._status_subscribed_at = None
        self._status.speed = 0.0
        self._status.belt_state = BeltState.STOPPED
        self._wake_waiters()
        if changed:
            self._notify_status()

    def _wake_waiters(self) -> None:
        """Wake every pending wait so it sees the disconnect immediately.

        Waiters re-check `connected` after waking, so a spurious set is
        harmless; each write clears its events before use.
        """
        self._treadmill_data_event.set()
        self._cp_response_event.set()
        self._status_ack_event.set()
        self._session_end_event.set()

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
        (100/200/300 ms); we wait 100 ms after Treadmill Data and 200 ms after
        FM Status. See `docs/ftms-protocol-reference.md` §2.1.
        """
        if not self._client:
            return

        # Minimal connect-path subscription set:
        #   - Treadmill Data: live speed/distance/etc (essential)
        #   - Fitness Machine Status: command-applied notifications (essential
        #     for verifying start/stop/set-speed without relying solely on
        #     CP indications)
        #   - Control Point: indications for command-and-ack flow (essential)
        #
        # Training Status (0x2AD3) was removed from the connect path. It's
        # informational only (PRE_WORKOUT / MANUAL / etc), the lib doesn't
        # gate any decision on it, and dropping it cuts one CCCD write from
        # the critical setup. Callers who want it can call
        # subscribe_training_status() lazily.
        # Without Treadmill Data or Control Point indications no command can
        # be confirmed, so a failed subscription fails the connect (and the
        # caller retries) instead of leaving a half-working link.
        subscriptions = (
            _Subscription(
                TREADMILL_DATA_UUID, self._on_treadmill_data, "Treadmill Data", 0.10, True
            ),
            # On the vendor pre-amble path FM Status is the only ack channel.
            _Subscription(
                FITNESS_MACHINE_STATUS_UUID,
                self._on_machine_status,
                "Fitness Machine Status",
                0.20,
                self._capabilities.has_vendor_preamble,
            ),
            _Subscription(
                FTMS_CONTROL_POINT_UUID,
                self._on_control_point_response,
                "Control Point",
                0.0,
                True,
            ),
        )
        assert tuple(sub.uuid for sub in subscriptions) == _CONNECT_SUBSCRIPTION_UUIDS

        for uuid, handler, label, delay_after, required in subscriptions:
            try:
                # Stamp before the CCCD write: the replay can be dispatched
                # while start_notify is still awaiting.
                if uuid == FITNESS_MACHINE_STATUS_UUID:
                    self._status_subscribed_at = time.monotonic()
                await self._client.start_notify(uuid, handler)
                _LOGGER.debug("FTMS: Subscribed to %s", label)
            except Exception as err:
                if required:
                    raise BleakError(
                        f"FTMS: Failed to subscribe to {label}: {err}"
                    ) from err
                _LOGGER.warning("FTMS: Failed to subscribe to %s: %s", label, err)
            if delay_after:
                await asyncio.sleep(delay_after)

    async def _ensure_telemetry(self) -> None:
        """Make sure notifications flow; resubscribe once if they don't.

        Only used on devices known to stream Treadmill Data while idle (the
        R3), so silence usually means the subscription was dropped. If
        nothing arrives after resubscribing either, log and carry on: the
        belt may be in a silent idle state, and the Control Point still works.
        """
        for attempt in range(2):
            if await self._wait_for_frame(self._telemetry_check_timeout):
                return
            if not self.connected:
                raise BleakError("FTMS: link dropped during connection setup")
            if attempt == 0:
                _LOGGER.warning(
                    "FTMS: no Treadmill Data %.1fs after subscribing; resubscribing",
                    self._telemetry_check_timeout,
                )
                assert self._client is not None
                for uuid in _CONNECT_SUBSCRIPTION_UUIDS:
                    try:
                        await self._client.stop_notify(uuid)
                    except Exception as err:
                        _LOGGER.debug("FTMS: stop_notify %s failed: %s", uuid, err)
                # The resubscribe triggers a fresh 0x2ADA replay; drop it too.
                self._machine_status_first_event_seen = False
                await self._subscribe_notifications()
        _LOGGER.warning(
            "FTMS: still no Treadmill Data after resubscribing; continuing"
        )

    async def _wait_for_next_frame(self, timeout: float) -> None:
        """Sleep until the next Treadmill Data frame (or disconnect/timeout).

        Callers check their condition right before this, with no await in
        between, so clearing the event here can't lose a wake-up.
        """
        self._treadmill_data_event.clear()
        await _wait_event(self._treadmill_data_event, timeout)

    async def _wait_for_frame(self, timeout: float) -> bool:
        """Wait for any Treadmill Data frame; False on timeout or disconnect."""
        seq = self._treadmill_data_seq
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.connected and self._treadmill_data_seq == seq:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await self._wait_for_next_frame(remaining)
        return self.connected and self._treadmill_data_seq != seq

    async def subscribe_training_status(self) -> bool:
        """Subscribe to Training Status (0x2AD3) to fill `status.training_status`.

        Not part of the connect path (one CCCD write less on marginal links).
        Returns False if not connected or the subscription failed.
        """
        if not self.connected:
            return False
        assert self._client is not None
        try:
            await self._client.start_notify(TRAINING_STATUS_UUID, self._on_training_status)
        except Exception as err:
            _LOGGER.warning("FTMS: Failed to subscribe to Training Status: %s", err)
            return False
        return True

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
        self._treadmill_data_seq += 1
        if self._status.speed > 0:
            self._last_moving_seq = self._treadmill_data_seq
        self._treadmill_data_event.set()
        # Belt state derivation:
        #   speed > 0           -> ACTIVE
        #   speed = 0 + paused  -> PAUSED (session alive, awaiting resume)
        #   speed = 0 + ~paused -> STOPPED (session ended or never started)
        #
        # `_paused` is set/cleared exclusively by `_on_machine_status` in
        # response to FM Status events — NOT here. The previous version
        # cleared it on every speed > 0 frame, which raced with the
        # deceleration window after PAUSE: the FM Status PAUSE event
        # would set _paused = True at speed ≈ 2.5, then every Treadmill
        # Data frame during the 5-10 s decel cleared it again, leaving
        # _paused = False by the time speed reached 0 and belt_state
        # mis-reporting STOPPED instead of PAUSED.
        if speed_raw > 0:
            self._status.belt_state = BeltState.ACTIVE
        elif self._paused:
            self._status.belt_state = BeltState.PAUSED
        else:
            self._status.belt_state = BeltState.STOPPED
        offset += 2

        # Optional fields in spec order: (flag, size in bytes, handler).
        # A frame too short for a flagged field is truncated: stop there
        # instead of reading the following fields from the wrong offset.
        fields = (
            (TreadmillDataFlags.AVERAGE_SPEED, 2, None),  # UINT16
            (TreadmillDataFlags.TOTAL_DISTANCE, 3, self._parse_distance),  # UINT24 m
            (TreadmillDataFlags.INCLINATION, 4, None),  # INT16 + INT16
            (TreadmillDataFlags.ELEVATION_GAIN, 4, None),  # UINT16 + UINT16
            (TreadmillDataFlags.INSTANTANEOUS_PACE, 1, None),  # UINT8
            (TreadmillDataFlags.AVERAGE_PACE, 1, None),  # UINT8
            (TreadmillDataFlags.EXPENDED_ENERGY, 5, self._parse_energy),  # U16+U16+U8
            (TreadmillDataFlags.HEART_RATE, 1, self._parse_heart_rate),  # UINT8
            (TreadmillDataFlags.METABOLIC_EQUIVALENT, 1, None),  # UINT8
            (TreadmillDataFlags.ELAPSED_TIME, 2, self._parse_elapsed),  # UINT16 s
            (TreadmillDataFlags.REMAINING_TIME, 2, None),  # UINT16
            (TreadmillDataFlags.FORCE_ON_BELT, 4, None),  # INT16 + INT16
            # KingSmith extension (bit 13): uint16 LE step count + 1 zero byte
            (TreadmillDataFlags.KINGSMITH_EXTENSION, 3, self._parse_steps),
        )
        for flag, size, handler in fields:
            if not flags & flag:
                continue
            if offset + size > len(data):
                _LOGGER.debug(
                    "FTMS: Truncated Treadmill Data (flags=0x%04x, %d bytes)",
                    flags,
                    len(data),
                )
                break
            if handler is not None:
                handler(data, offset)
            offset += size

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

    def _parse_distance(self, data: bytearray, offset: int) -> None:
        self._status.distance = (
            data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
        )

    def _parse_energy(self, data: bytearray, offset: int) -> None:
        self._status.calories = struct.unpack_from("<H", data, offset)[0]
        self._status.calories_per_hour = struct.unpack_from("<H", data, offset + 2)[0]

    def _parse_heart_rate(self, data: bytearray, offset: int) -> None:
        self._status.heart_rate = data[offset]

    def _parse_elapsed(self, data: bytearray, offset: int) -> None:
        self._status.duration = struct.unpack_from("<H", data, offset)[0]

    def _parse_steps(self, data: bytearray, offset: int) -> None:
        self._status.steps = struct.unpack_from("<H", data, offset)[0]

    def _on_machine_status(self, sender: int, data: bytearray) -> None:
        """Handle Fitness Machine Status (2ADA) notifications.

        On KingSmith firmware that uses the vendor pre-amble (e.g. MC-21),
        these events are how command success is signalled — the device
        sends *no* indication on the Control Point itself for opcodes other
        than REQUEST_CONTROL.

        Order matters: count session ends, drop a stale replay, signal a
        pending ack, apply the state change, then publish.
        """
        if len(data) < 1:
            return

        opcode = data[0]
        _LOGGER.debug(
            "FTMS: Machine status event: 0x%02x (data: %s)", opcode, data.hex()
        )

        # Count session ends before the replay check: _send_stop() only looks
        # at the change across its own STOP write, so a stale replay counted
        # at subscribe time does no harm, while dropping a real 02 01 would
        # make a successful STOP look failed.
        if (
            opcode == FitnessMachineStatusOpcode.STOPPED_OR_PAUSED
            and len(data) >= 2
            and data[1] == FTMSStopPauseParam.STOP
        ):
            self._session_end_events += 1
            self._session_end_event.set()

        if self._is_stale_status_replay(opcode):
            return

        # Wake any pending command waiter that's expecting this opcode.
        if (
            self._status_ack_expected_opcode is not None
            and opcode == self._status_ack_expected_opcode
            and (
                self._status_ack_expected_param is None
                or (len(data) >= 2 and data[1] == self._status_ack_expected_param)
            )
        ):
            self._status_ack_event.set()

        self._apply_machine_status(opcode, data)

        # Record the most-recent processed event so callers can read it via
        # `controller.status.last_fm_event`, and fan out to the status
        # callbacks (e.g. HA coordinator) once all state above is updated.
        self._status.last_fm_event = opcode
        self._status.timestamp = time.time()
        self._notify_status()

    def _is_stale_status_replay(self, opcode: int) -> bool:
        """Return True for the firmware's replay of its state on subscribe.

        The first 2ADA event after subscribing is often the device replaying
        its prior state (typically a stale STOPPED_OR_PAUSED). Only that
        first event is considered, and only if it arrives within
        `_STATUS_REPLAY_WINDOW` of subscribing and doesn't match the ack
        opcode a pending command expects (e.g. pause() right after connect,
        whose PAUSE event may be the first one).
        """
        if self._machine_status_first_event_seen:
            return False
        self._machine_status_first_event_seen = True
        late = (
            self._status_subscribed_at is None
            or time.monotonic() - self._status_subscribed_at > _STATUS_REPLAY_WINDOW
        )
        if late:
            # Too long after subscribing to be the replay. On devices
            # without the vendor pre-amble there is never an expected ack
            # opcode, so this is how their first real event survives.
            return False
        if (
            self._status_ack_expected_opcode is not None
            and opcode == self._status_ack_expected_opcode
        ):
            _LOGGER.debug(
                "FTMS: First 2ADA event after subscribe matches expected "
                "ack opcode 0x%02x — treating as real ack, not stale replay",
                opcode,
            )
            return False
        _LOGGER.debug(
            "FTMS: Ignoring first 2ADA event after subscribe "
            "(opcode 0x%02x, no matching pending command)",
            opcode,
        )
        return True

    def _apply_machine_status(self, opcode: int, data: bytearray) -> None:
        """Update pause/belt state (and log) for one FM Status event."""
        if opcode == FitnessMachineStatusOpcode.STOPPED_OR_PAUSED:
            if len(data) >= 2:
                if data[1] == FTMSStopPauseParam.STOP:
                    _LOGGER.info("FTMS: Treadmill stopped by user")
                    self._paused = False
                    # While moving, Treadmill Data with speed=0 follows and
                    # sets STOPPED. At rest (e.g. ending a paused session)
                    # no further frame may come, so set it here.
                    if self._status.speed == 0:
                        self._status.belt_state = BeltState.STOPPED
                elif data[1] == FTMSStopPauseParam.PAUSE:
                    _LOGGER.info("FTMS: Treadmill paused by user")
                    self._paused = True
                    # While decelerating, Treadmill Data keeps belt_state
                    # ACTIVE and switches to PAUSED at 0 km/h.
                    if self._status.speed == 0:
                        self._status.belt_state = BeltState.PAUSED
        elif opcode == FitnessMachineStatusOpcode.STOPPED_BY_SAFETY_KEY:
            _LOGGER.info("FTMS: Treadmill stopped by safety key")
            self._paused = False
            if self._status.speed == 0:
                self._status.belt_state = BeltState.STOPPED
        elif opcode == FitnessMachineStatusOpcode.STARTED_OR_RESUMED:
            _LOGGER.info("FTMS: Treadmill started/resumed by user")
            # Treadmill Data never clears `_paused`; only FM Status events
            # (and a disconnect) do.
            self._paused = False
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

        # Push the new status code into TreadmillStatus and fan out so
        # downstream consumers (HA coordinator) see the change.
        if self._status.training_status != status_code:
            self._status.training_status = status_code
            self._status.timestamp = time.time()
            self._notify_status()

    def _on_control_point_response(self, sender: int, data: bytearray) -> None:
        """Handle FTMS Control Point (2AD9) indication responses.

        Response format: [0x80, request_opcode, result_code, ...]
        """
        _LOGGER.debug("FTMS: Control Point response: %s", data.hex())
        if (
            self._cp_pending_opcode is not None
            and len(data) >= 2
            and data[0] == FTMSOpcode.RESPONSE_CODE
            and data[1] != self._cp_pending_opcode
        ):
            # Late indication for an earlier command: don't let it overwrite
            # the slot the current command is waiting on.
            _LOGGER.debug("FTMS: Ignoring indication for opcode 0x%02x", data[1])
            return
        self._cp_response_data = bytes(data)
        self._cp_response_event.set()

    # --- Control Commands ---

    async def _write_control_point(
        self, opcode: FTMSOpcode, params: bytes = b"", timeout: float = _CP_ACK_TIMEOUT
    ) -> bool:
        """Write a command to the FTMS Control Point and wait for response.

        Returns True if the command was acknowledged with success. On the
        vendor pre-amble path a missing ack also counts as success (see
        `_write_and_wait`).
        """
        result = await self._write_control_point_result(opcode, params, timeout)
        if result is _CPResult.TIMEOUT:
            return self._capabilities.has_vendor_preamble
        return result is _CPResult.ACK

    async def _write_control_point_result(
        self, opcode: FTMSOpcode, params: bytes = b"", timeout: float = _CP_ACK_TIMEOUT
    ) -> _CPResult:
        """Like `_write_control_point`, but report how the write ended.

        Writes are serialised: the response slot is shared, so a second
        command must not clear or consume the first one's acknowledgement.
        """
        async with self._cp_lock:
            try:
                return await self._write_and_wait(opcode, params, timeout)
            finally:
                # Always clear the expectation, also on error or cancellation.
                self._status_ack_expected_opcode = None
                self._status_ack_expected_param = None
                self._cp_pending_opcode = None

    async def _write_and_wait(
        self, opcode: FTMSOpcode, params: bytes, timeout: float
    ) -> _CPResult:
        """Body of `_write_control_point_result`; caller holds `_cp_lock`."""
        if not self.connected:
            _LOGGER.warning("FTMS: Not connected, cannot send command")
            return _CPResult.NOT_SENT
        client = self._client
        assert client is not None

        if self._capabilities.has_vendor_preamble:
            await self._write_vendor_preamble(client)
        expected_status_opcode = self._arm_status_ack(opcode, params)

        command = bytes([opcode]) + params
        _LOGGER.debug("FTMS: Sending control point command: %s", command.hex())

        self._cp_response_event.clear()
        # Known protocol limit: an indication carries only the request
        # opcode, so a late ack of an earlier command with the same opcode
        # (PAUSE vs STOP) cannot be told apart from this command's ack.
        self._cp_pending_opcode = opcode

        try:
            await client.write_gatt_char(
                FTMS_CONTROL_POINT_UUID, command, response=True
            )
        except BleakError as err:
            _LOGGER.warning("FTMS: Write error: %s", err)
            return _CPResult.NOT_SENT

        result = await self._await_cp_ack(opcode, expected_status_opcode, timeout)
        if result is not None:
            return result

        # Neither arrived in time. On the pre-amble path the firmware often
        # applies a command without any ack (issue #1), so
        # `_write_control_point` treats this as success there.
        if self._capabilities.has_vendor_preamble:
            _LOGGER.debug(
                "FTMS: No ack for opcode 0x%02x (vendor pre-amble path)", opcode
            )
        else:
            _LOGGER.warning(
                "FTMS: Control point response timeout for opcode 0x%02x", opcode
            )
        return _CPResult.TIMEOUT

    async def _write_vendor_preamble(self, client: BleakClient) -> None:
        """Send the KingSmith vendor pre-amble; errors are only logged."""
        try:
            await client.write_gatt_char(
                KINGSMITH_VENDOR_PREAMBLE_UUID,
                KINGSMITH_VENDOR_PREAMBLE_PAYLOAD,
                response=True,
            )
        except BleakError as err:
            _LOGGER.debug("FTMS: Vendor pre-amble write error: %s", err)

    def _arm_status_ack(self, opcode: FTMSOpcode, params: bytes) -> int | None:
        """Set up the FM Status ack expectation; return its opcode, if any.

        On the pre-amble path, the device acknowledges most opcodes via a
        Fitness Machine Status (2ADA) event rather than a Control Point
        indication, so the status handler signals us when the matching
        event arrives. STOP_OR_PAUSE is acked by STOPPED_OR_PAUSED with the
        same parameter, so a PAUSE event can't acknowledge a STOP.
        """
        expected_status_opcode = (
            _CP_TO_STATUS_ACK.get(opcode)
            if self._capabilities.has_vendor_preamble
            else None
        )
        if expected_status_opcode is not None:
            self._status_ack_expected_opcode = expected_status_opcode
            self._status_ack_expected_param = (
                params[0] if opcode == FTMSOpcode.STOP_OR_PAUSE and params else None
            )
        self._status_ack_event.clear()
        return expected_status_opcode

    async def _await_cp_ack(
        self, opcode: FTMSOpcode, expected_status_opcode: int | None, timeout: float
    ) -> _CPResult | None:
        """Wait for this command's ack; None if nothing arrived in time.

        Waits for either a Control Point indication OR a matching Fitness
        Machine Status event. On standard FTMS firmware the indication
        arrives quickly (sub-second). On the pre-amble path most opcodes
        only get a 2ADA event; first-call REQUEST_CONTROL still gets an
        indication. Race them so whichever arrives first wins. A late
        indication for an earlier command is ignored and we keep waiting.
        """
        effective_timeout = (
            _CP_ACK_TIMEOUT_PREAMBLE if self._capabilities.has_vendor_preamble else timeout
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + effective_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            waiters = [asyncio.create_task(self._cp_response_event.wait())]
            if expected_status_opcode is not None:
                waiters.append(asyncio.create_task(self._status_ack_event.wait()))
            try:
                await asyncio.wait(
                    waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for w in waiters:
                    if not w.done():
                        w.cancel()

            if not self.connected:
                _LOGGER.warning(
                    "FTMS: Connection lost waiting for the ack of opcode 0x%02x",
                    opcode,
                )
                return _CPResult.NOT_SENT

            # Status event won the race → command was acknowledged.
            if expected_status_opcode is not None and self._status_ack_event.is_set():
                _LOGGER.debug("FTMS: Status-event ack for opcode 0x%02x", opcode)
                return _CPResult.ACK

            if not self._cp_response_event.is_set():
                return None  # timed out

            result = self._parse_cp_response(opcode)
            if result is not None:
                return result
            self._cp_response_event.clear()  # late indication; keep waiting

    def _parse_cp_response(self, opcode: FTMSOpcode) -> _CPResult | None:
        """Interpret the indication in the response slot for ``opcode``.

        Format: [0x80, request_opcode, result_code]. Returns None for an
        indication that belongs to another opcode.
        """
        resp = self._cp_response_data
        if len(resp) >= 3 and resp[0] == FTMSOpcode.RESPONSE_CODE:
            req_opcode = resp[1]
            result = resp[2]
            if req_opcode != opcode:
                _LOGGER.debug(
                    "FTMS: Ignoring late indication for opcode 0x%02x while "
                    "waiting for 0x%02x",
                    req_opcode,
                    opcode,
                )
                return None
            if result == FTMSResultCode.SUCCESS:
                _LOGGER.debug("FTMS: Command 0x%02x succeeded", opcode)
                return _CPResult.ACK
            _LOGGER.warning("FTMS: Command 0x%02x result: %d", opcode, result)
            return _CPResult.REJECTED

        _LOGGER.warning("FTMS: Unexpected control point response: %s", resp.hex())
        return _CPResult.REJECTED

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

    async def _ensure_control_requested(self) -> None:
        """Send REQUEST_CONTROL once per connection (its result is ignored)."""
        if not self._has_control:
            await self._request_control()

    async def start(self) -> bool:
        """Start or resume the treadmill belt.

        Sends START_OR_RESUME and, on a cold start, waits for the belt to
        report speed > 0.  Does NOT send SET_TARGET_SPEED — the user sets
        the speed explicitly via set_target_speed() (e.g. the HA speed
        slider).  Sending a speed command during motor spin-up crashes the
        BLE connection on KingSmith firmware.

        Returns:
            True if the belt is running. False if the connection was lost,
            START was rejected while the belt is at rest, the belt didn't
            move in time, or pause() was called meanwhile.
        """
        async with self._op_lock:
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        pause_seq = self._pause_seq
        await self._ensure_control_requested()

        if not self.connected:
            _LOGGER.warning("FTMS: Not connected; cannot start")
            return False
        if self._pause_seq != pause_seq:
            _LOGGER.info("FTMS: Paused before START was sent; start abandoned")
            return False

        seq = self._treadmill_data_seq
        accepted = await self._write_control_point(FTMSOpcode.START_OR_RESUME)

        if not self.connected:
            _LOGGER.warning("FTMS: Connection lost during START_OR_RESUME")
            return False

        if accepted:
            _LOGGER.info("FTMS: START_OR_RESUME accepted (cold start)")
            belt_running = await self._wait_for_belt_moving(
                seq, timeout=_START_MOTION_TIMEOUT, pause_seq=pause_seq
            )
            if not belt_running:
                if not self.connected:
                    _LOGGER.warning("FTMS: Connection lost waiting for belt to start")
                    return False
                if self._pause_seq != pause_seq:
                    _LOGGER.info("FTMS: Paused while starting; start abandoned")
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
        # Disambiguate by looking at fresh telemetry; the cached speed may be
        # left over from before a reconnect.
        outcome = await self._wait_for_motion(
            seq, self._start_rejected_grace, pause_seq, latch=False
        )
        # A decelerating belt after PAUSE still reports speed > 0.
        if (
            outcome is _Standstill.STILL_MOVING
            and not self._paused
            and self._pause_seq == pause_seq
        ):
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

    async def _wait_for_belt_moving(
        self, after_seq: int, timeout: float = 15.0, pause_seq: int | None = None
    ) -> bool:
        """Wait for a Treadmill Data frame newer than ``after_seq`` with speed > 0.

        No stabilisation delay is applied here — we deliberately avoid
        sending any speed command while the connection is fragile.

        Returns True if the belt is moving, False on timeout/disconnect or
        when pause() was called since ``pause_seq``.
        """
        _LOGGER.debug(
            "FTMS: Waiting for belt to start moving (timeout=%.0fs)...", timeout
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = await self._wait_for_motion(after_seq, timeout, pause_seq, latch=False)
        if outcome is not _Standstill.STILL_MOVING:
            return False
        _LOGGER.info(
            "FTMS: Belt moving at %.1f km/h (waited %.1fs)",
            self._status.speed,
            loop.time() - started,
        )
        return True

    async def stop(self) -> bool:
        """Stop the treadmill — end the session.

        On most FTMS devices this sends STOP directly. Devices listed in
        ``FTMS_PAUSE_BEFORE_STOP_PREFIXES`` (the KingSmith R3) answer STOP
        with OPERATION_FAILED while the belt is moving and keep it running.
        For those, mirror KS Fit: send PAUSE, wait for Treadmill Data
        received *after* the PAUSE to report 0 km/h, then send STOP. The
        cached speed is never trusted for this decision; it may be stale or
        lag a physical/remote start.

        Returns:
            True if the session was ended. False if STOP was rejected or
            not sent because standstill could not be confirmed (in that
            case the belt has been asked to pause, but its state may be
            unknown — see the log).
        """
        async with self._op_lock:
            await self._ensure_control_requested()
            if not self._capabilities.pause_before_stop:
                return await self._send_stop()

            for attempt in range(1, self._stop_attempts + 1):
                if not await self._pause_until_standstill():
                    return False
                seq = self._treadmill_data_seq
                if await self._send_stop():
                    return True
                if not self.connected:
                    _LOGGER.warning("FTMS: connection lost while sending STOP")
                    return False
                # The R3 rejects both PAUSE and STOP at 0 km/h in two
                # states: no session at all, and the 3-2-1 countdown after
                # a START (from us, the remote or the app). In the
                # countdown case the belt starts a moment later anyway and
                # cannot be halted before it does, so watch for motion and
                # pause it as soon as it moves.
                if attempt == self._stop_attempts:
                    break
                outcome = await self._wait_for_motion(seq, self._countdown_grace)
                if outcome is _Standstill.DISCONNECTED:
                    _LOGGER.warning(
                        "FTMS: connection lost after STOP was rejected; belt "
                        "state unknown"
                    )
                    return False
                if outcome is not _Standstill.STILL_MOVING:
                    _LOGGER.warning(
                        "FTMS: STOP rejected and the belt stayed at rest "
                        "(no active session?)"
                    )
                    return False
                _LOGGER.warning(
                    "FTMS: belt started after STOP was rejected (start "
                    "countdown); pausing it (attempt %d/%d)",
                    attempt + 1,
                    self._stop_attempts,
                )
            _LOGGER.warning("FTMS: STOP rejected; session not ended")
            return False

    async def _send_stop(self) -> bool:
        """Send STOP; also accept the FM Status session-end event as success.

        The R3 occasionally drops the Control Point indication even though
        the command took effect, while still emitting FM Status ``02 01``.
        """
        ends_before = self._session_end_events
        self._session_end_event.clear()
        result = await self._write_control_point_result(
            FTMSOpcode.STOP_OR_PAUSE,
            bytes([FTMSStopPauseParam.STOP]),
        )
        if result is _CPResult.ACK:
            return True
        if (
            result is _CPResult.TIMEOUT
            and self._session_end_events == ends_before
            and self.connected
        ):
            # No ack at all: the session-end event may still be on its way.
            await _wait_event(self._session_end_event, self._session_end_grace)
        # Only a missing ack is overridden by FM Status; an explicit
        # rejection stands.
        if result is _CPResult.TIMEOUT and self._session_end_events != ends_before:
            _LOGGER.info(
                "FTMS: STOP not acknowledged on the Control Point, but FM "
                "Status reported the session ended"
            )
            return True
        # Pre-amble firmware often applies commands without any ack.
        return (
            result is _CPResult.TIMEOUT
            and self._capabilities.has_vendor_preamble
            and self.connected
        )

    async def _pause_until_standstill(self) -> bool:
        """Send PAUSE and wait for fresh telemetry to confirm 0 km/h.

        PAUSE is sent regardless of the cached speed. A rejected or
        unacknowledged PAUSE is not fatal on its own (the belt may already
        be paused, or the indication was dropped): the decision rests on
        Treadmill Data received after the PAUSE.
        """
        seq = self._treadmill_data_seq
        if not await self._write_control_point(
            FTMSOpcode.STOP_OR_PAUSE,
            bytes([FTMSStopPauseParam.PAUSE]),
        ):
            _LOGGER.info(
                "FTMS: PAUSE not acknowledged; checking telemetry before STOP"
            )
        outcome = await self._wait_for_standstill(seq, self._standstill_timeout)
        if outcome is _Standstill.CONFIRMED:
            return True
        if outcome is _Standstill.DISCONNECTED:
            _LOGGER.warning(
                "FTMS: connection lost while waiting for the belt to stop "
                "after PAUSE; belt state unknown, STOP not sent"
            )
        elif outcome is _Standstill.NO_TELEMETRY:
            _LOGGER.warning(
                "FTMS: no Treadmill Data within %.0fs of PAUSE; belt state "
                "unknown, STOP not sent",
                self._standstill_timeout,
            )
        else:
            _LOGGER.warning(
                "FTMS: belt still reports %.1f km/h %.0fs after PAUSE; STOP "
                "not sent",
                self._status.speed,
                self._standstill_timeout,
            )
        return False

    async def _wait_for_motion(
        self,
        after_seq: int,
        timeout: float,
        pause_seq: int | None = None,
        *,
        latch: bool = True,
    ) -> _Standstill:
        """Wait for a Treadmill Data frame newer than ``after_seq`` with speed > 0.

        With ``latch`` (stop()'s countdown watch), any moving frame since
        ``after_seq`` counts, even if a 0 km/h frame followed before we woke.
        Without it (start()), the latest frame must still report motion.

        Returns STILL_MOVING if the belt moved, CONFIRMED if it stayed at
        rest until the timeout (or, with ``pause_seq``, until pause() was
        called), or DISCONNECTED.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not self.connected:
                return _Standstill.DISCONNECTED
            if pause_seq is not None and self._pause_seq != pause_seq:
                return _Standstill.CONFIRMED
            if latch and self._last_moving_seq > after_seq:
                return _Standstill.STILL_MOVING
            if self._treadmill_data_seq > after_seq and self._status.speed > 0:
                return _Standstill.STILL_MOVING
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _Standstill.CONFIRMED
            await self._wait_for_next_frame(remaining)

    async def _wait_for_standstill(self, after_seq: int, timeout: float) -> _Standstill:
        """Wait for a Treadmill Data frame newer than ``after_seq`` with speed 0."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not self.connected:
                return _Standstill.DISCONNECTED
            if self._treadmill_data_seq > after_seq and self._status.speed == 0:
                return _Standstill.CONFIRMED
            remaining = deadline - loop.time()
            if remaining <= 0:
                if self._treadmill_data_seq == after_seq:
                    return _Standstill.NO_TELEMETRY
                return _Standstill.STILL_MOVING
            await self._wait_for_next_frame(remaining)

    async def pause(self) -> bool:
        """Pause the treadmill.

        Not serialised behind other operations (only behind other Control
        Point writes), so it can halt the belt while e.g. start() is still
        waiting for the belt to move.
        """
        self._pause_seq += 1
        self._treadmill_data_event.set()  # let a waiting start() see it
        await self._ensure_control_requested()
        return await self._write_control_point(
            FTMSOpcode.STOP_OR_PAUSE,
            bytes([FTMSStopPauseParam.PAUSE]),
        )

    async def reset(self) -> bool:
        """Reset the fitness machine."""
        async with self._op_lock:
            await self._ensure_control_requested()
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

        # Round to the nearest increment, then clamp, so rounding can't push
        # the value outside the supported range.
        if sr.increment > 0:
            steps = round(speed_kmh / sr.increment)
            speed_kmh = steps * sr.increment
        speed_kmh = max(sr.min_speed, min(sr.max_speed, speed_kmh))

        # Convert to UINT16 in 0.01 km/h units
        speed_raw = int(round(speed_kmh * 100))
        params = struct.pack("<H", speed_raw)

        async with self._op_lock:
            await self._ensure_control_requested()

            _LOGGER.debug(
                "FTMS: Setting target speed to %.2f km/h (raw: %d)",
                speed_kmh,
                speed_raw,
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

        async with self._op_lock:
            await self._ensure_control_requested()

            return await self._write_control_point(
                FTMSOpcode.SET_TARGET_INCLINATION, params
            )
