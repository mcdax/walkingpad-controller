"""Sperax / wi-linktech (WLT6200) protocol implementation.

Implements the custom framed BLE protocol used by the **Sperax P3 Max** walking
pad (BLE name ``SPERAX_P3MAX``). The device is built around a wi-linktech
**WLT6200** module and does NOT speak FTMS (0x1826) nor the legacy KingSmith
WiLink protocol (0xFE00). Instead it exposes a vendor service (0xFFF0) with a
framed, CRC-checked protocol:

    F5 | LEN | 00 | CMD | [ARGS...] | CRC_lo | CRC_hi | FA

  - ``F5``/``FA``  : start/end delimiters
  - ``LEN``        : transmitted (post byte-stuffing) frame length
  - byte-stuffing  : any body byte 0xF0-0xFF is sent as ``F0 (byte & 0x0F)``
  - ``CRC``        : CRC-16, poly 0xA327, init 0xFFFF, reflected, little-endian,
                     computed over the *de-stuffed* ``F5 LOGICAL_LEN 00 CMD ARGS``

Commands (app -> device, char 0xFFF2, write-no-response):
  - 0x01 hello / handshake                 inner ``00 01``
  - 0x15 run control  ``00 15 <state> <speed> <incline>``
        state 0x01=run / 0x02=stop ; speed = km/h x 10 ; incline 0/1/2
  - 0x16 vibration    ``00 16 <state> <level>``   state 0x01=on/0x00=off, level 1-4
  - 0x19 status poll / keep-alive          inner ``00 19``

Status notifications (device -> app, char 0xFFF1, CMD 0x19) carry belt state,
current speed (km/h x 10), incline and vibration level, plus several cumulative
counters that are not yet fully separated.

Protocol reference: ``docs/sperax-p3max-protocol.md`` (verified against a full
HCI snoop capture — 146/146 distinct frames pass CRC).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from .const import (
    SPERAX_NOTIFY_UUID,
    SPERAX_WRITE_UUID,
    BeltState,
)
from .models import DeviceCapabilities, SpeedRange, TreadmillStatus

_LOGGER = logging.getLogger(__name__)


# --- Frame codec -----------------------------------------------------------

_SOF = 0xF5
_EOF = 0xFA
_ESC = 0xF0
_CRC_POLY = 0xA327  # reflected form
_CRC_INIT = 0xFFFF


def crc16(data: bytes) -> int:
    """CRC-16 used by the WLT6200 protocol (poly 0xA327, init 0xFFFF, reflected)."""
    c = _CRC_INIT
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ _CRC_POLY if (c & 1) else (c >> 1)
    return c & 0xFFFF


def _stuff(b: bytes) -> bytes:
    out = bytearray()
    for x in b:
        if _ESC <= x <= 0xFF:
            out += bytes([_ESC, x & 0x0F])
        else:
            out.append(x)
    return bytes(out)


def _destuff(b: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i] == _ESC and i + 1 < len(b):
            out.append(_ESC | b[i + 1])
            i += 2
        else:
            out.append(b[i])
            i += 1
    return bytes(out)


def encode(inner: bytes) -> bytes:
    """Frame a logical body ``[0x00, CMD, ARGS...]`` into an on-wire frame."""
    logical_len = 1 + 1 + len(inner) + 2 + 1  # F5 + LEN + inner + CRC(2) + FA
    c = crc16(bytes([_SOF, logical_len]) + inner)
    body = _stuff(inner + bytes([c & 0xFF, (c >> 8) & 0xFF]))
    tx_len = 1 + 1 + len(body) + 1
    return bytes([_SOF, tx_len]) + body + bytes([_EOF])


def decode(frame: bytes) -> bytes | None:
    """De-frame and CRC-check an on-wire frame.

    Returns the logical body ``[0x00, CMD, ARGS...]`` or ``None`` if the frame
    is malformed or fails the CRC check.
    """
    if len(frame) < 6 or frame[0] != _SOF or frame[-1] != _EOF:
        return None
    full = bytes([_SOF]) + _destuff(frame[1:-1]) + bytes([_EOF])
    if len(full) < 6:
        return None
    data = bytearray(full[:-3])
    data[1] = len(full)  # LOGICAL_LEN participates in the CRC
    lo, hi = full[-3], full[-2]
    if crc16(bytes(data)) != ((hi << 8) | lo):
        return None
    return full[2:-3]


# Command opcodes / helpers
_CMD_HELLO = 0x01
_CMD_RUN = 0x15
_CMD_VIBRATION = 0x16
_CMD_STATUS = 0x19
_CMD_ACK = 0xD0

_RUN_STATE_RUN = 0x01
_RUN_STATE_STOP = 0x02
_VIB_STATE_ON = 0x01
_VIB_STATE_OFF = 0x00

# How often to poll the device. The WLT6200 only streams status while it is
# being polled; the official app polls ~3x/s. 0.5 s keeps the link alive and
# the status fresh without hammering the write characteristic.
_POLL_INTERVAL = 0.5

# Speed capabilities. The reference capture only exercised up to 3.0 km/h;
# the P3 Max spec sheet advertises 6.0 km/h. These are best-effort defaults
# until confirmed on hardware — the device is not known to expose a readable
# speed-range characteristic.
_MIN_SPEED = 0.5
_MAX_SPEED = 6.0
_SPEED_INCREMENT = 0.1
_MAX_INCLINE = 2
_MAX_VIBRATION = 4


class SperaxController:
    """Controller for Sperax P3 Max (wi-linktech WLT6200) walking pads."""

    def __init__(self) -> None:
        self._client: BleakClient | None = None
        self._connected = False
        self._status = TreadmillStatus()
        self._capabilities = DeviceCapabilities(
            speed_range=SpeedRange(
                min_speed=_MIN_SPEED, max_speed=_MAX_SPEED, increment=_SPEED_INCREMENT
            )
        )
        self._status_callbacks: list[Callable[[TreadmillStatus], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []

        # Desired belt state. Every run command carries speed AND incline, so
        # we track both and re-send the full command on any change.
        self._target_speed_tenths = int(round(_MIN_SPEED * 10))
        self._target_incline = 0
        self._vibration_level = 0

        self._poll_task: asyncio.Task | None = None

    # --- Properties ---

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
        """Firmware version — not read from this device yet."""
        return self._capabilities.firmware_version

    @property
    def vibration_level(self) -> int:
        """Last known vibration level (0 = off, 1-4)."""
        return self._vibration_level

    def register_status_callback(
        self, callback: Callable[[TreadmillStatus], None]
    ) -> None:
        """Register a callback for status updates."""
        self._status_callbacks.append(callback)

    def register_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for disconnect events."""
        self._disconnect_callbacks.append(callback)

    def _notify_status(self) -> None:
        for cb in self._status_callbacks:
            try:
                cb(self._status)
            except Exception:
                _LOGGER.exception("Error in status callback")

    # --- Connection ---

    async def connect(self, ble_device: BLEDevice) -> None:
        """Connect, subscribe to status, send hello, and start polling."""
        _LOGGER.info("Sperax: Connecting to %s", ble_device.address)
        self._client = await establish_connection(
            BleakClient,
            ble_device,
            ble_device.name or ble_device.address,
            disconnected_callback=self._on_disconnect,
        )
        self._connected = True
        _LOGGER.info("Sperax: Connected to %s", ble_device.address)

        # Subscribe to status notifications (0xFFF1) then say hello (0x01).
        await self._client.start_notify(SPERAX_NOTIFY_UUID, self._on_notify)
        await self._write(bytes([0x00, _CMD_HELLO]))

        # The device only streams status while polled — start the keep-alive.
        self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())

        if not self.connected:
            raise BleakError(
                "Sperax: BLE link dropped during connection setup; treating "
                "as a failed connect."
            )

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        self._stop_poll()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
        self._connected = False

    def _on_disconnect(self, client: BleakClient) -> None:
        _LOGGER.warning("Sperax: Device disconnected")
        self._connected = False
        self._stop_poll()
        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in disconnect callback")

    def _stop_poll(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """Periodically poll the device so it keeps streaming status."""
        try:
            while self.connected:
                try:
                    await self._write(bytes([0x00, _CMD_STATUS]))
                except BleakError as err:
                    _LOGGER.debug("Sperax: poll write failed: %s", err)
                    break
                await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            pass

    # --- Write helper ---

    async def _write(self, inner: bytes) -> None:
        """Frame ``inner`` and write it to the command characteristic."""
        if not self._client:
            raise BleakError("Sperax: not connected")
        await self._client.write_gatt_char(
            SPERAX_WRITE_UUID, encode(inner), response=False
        )

    # --- Notification handling ---

    def _on_notify(self, sender: int, data: bytearray) -> None:
        inner = decode(bytes(data))
        if inner is None or len(inner) < 2:
            return
        cmd = inner[1]
        if cmd == _CMD_STATUS:
            self._parse_status(inner)
        elif cmd == _CMD_ACK:
            _LOGGER.debug("Sperax: command ack %s", inner.hex())
        elif cmd == _CMD_HELLO:
            _LOGGER.debug("Sperax: hello response %s", inner.hex())

    def _parse_status(self, inner: bytes) -> None:
        """Parse a 0x19 status body ``[0x00, 0x19, ...]`` into TreadmillStatus.

        Byte offsets within ``inner`` (see docs/sperax-p3max-protocol.md §5.2):
          4  state (0x10 run, 0x0F decel, 0x01 stopped, 0x50 vibration, 0x00 idle)
          8  counter A (elapsed-time-like)   14 counter D (steps/distance-like)
          15 speed x10   16 incline   18 vibration level

        Only speed / belt-state / vibration are confidently decoded; the
        cumulative counters are not yet separated (time vs distance vs steps
        vs calories), so distance/duration/steps are left at their captured
        raw counters as a best-effort and clearly flagged here.
        """
        if len(inner) < 19:
            return

        speed_raw = inner[15]
        self._status.speed = speed_raw / 10.0
        if speed_raw > 0:
            self._status.belt_state = BeltState.ACTIVE
        else:
            self._status.belt_state = BeltState.STOPPED

        self._vibration_level = inner[18]

        # Best-effort counters — NOT yet unit-verified. Exposed so callers have
        # *something* trending; treat with caution until confirmed on hardware.
        # counter D (offset 14) trends fastest (~steps/distance).
        self._status.steps = inner[14]
        # counter A (offset 8) trends ~1/s (~elapsed seconds).
        self._status.duration = inner[8]

        self._status.timestamp = time.time()
        self._notify_status()

    # --- Commands ---

    async def _send_run(self) -> bool:
        """(Re)send the current run target (speed + incline)."""
        speed = max(
            0,
            min(int(round(self._max_speed_tenths())), self._target_speed_tenths),
        )
        inner = bytes(
            [0x00, _CMD_RUN, _RUN_STATE_RUN, speed & 0xFF, self._target_incline & 0xFF]
        )
        try:
            await self._write(inner)
            return True
        except BleakError as err:
            _LOGGER.warning("Sperax: run command failed: %s", err)
            return False

    def _max_speed_tenths(self) -> int:
        return int(round(self.max_speed * 10))

    async def start(self) -> bool:
        """Start the belt at the minimum speed.

        Mirrors the other backends: start does not jump straight to a high
        speed. Callers set the desired speed afterwards via set_target_speed().
        """
        self._target_speed_tenths = max(
            int(round(self.min_speed * 10)), self._target_speed_tenths
        )
        return await self._send_run()

    async def stop(self) -> bool:
        """Stop the belt (``15 02 00 00``)."""
        try:
            await self._write(bytes([0x00, _CMD_RUN, _RUN_STATE_STOP, 0x00, 0x00]))
            return True
        except BleakError as err:
            _LOGGER.warning("Sperax: stop failed: %s", err)
            return False

    async def pause(self) -> bool:
        """Pause the belt.

        The WLT6200 protocol has no separate pause opcode, so this falls back
        to stop() — same approach the WiLink backend takes.
        """
        _LOGGER.debug("Sperax: no pause opcode; falling back to stop")
        return await self.stop()

    async def set_target_speed(self, speed_kmh: float) -> bool:
        """Set the belt speed in km/h."""
        speed_kmh = max(self.min_speed, min(self.max_speed, speed_kmh))
        self._target_speed_tenths = int(round(speed_kmh * 10))
        return await self._send_run()

    async def set_target_inclination(self, incline_step: int) -> bool:
        """Set the incline as a step (0-2).

        Unlike FTMS (which uses a percentage), the P3 Max exposes incline as
        discrete steps. The value is clamped to 0..2.
        """
        self._target_incline = max(0, min(_MAX_INCLINE, int(incline_step)))
        return await self._send_run()

    async def set_vibration(self, level: int) -> bool:
        """Set the vibration level (0 = off, 1-4).

        Note: the belt and the vibration motor are mutually exclusive — turning
        vibration on stops the belt.
        """
        level = max(0, min(_MAX_VIBRATION, int(level)))
        state = _VIB_STATE_ON if level > 0 else _VIB_STATE_OFF
        try:
            await self._write(bytes([0x00, _CMD_VIBRATION, state, level & 0xFF]))
            self._vibration_level = level
            return True
        except BleakError as err:
            _LOGGER.warning("Sperax: set vibration failed: %s", err)
            return False

    async def switch_mode(self, mode: int) -> bool:
        """Switch operating mode (compat shim).

        The P3 Max has no auto/manual mode concept; STANDBY (2) maps to stop.
        """
        from .const import OperatingMode

        if mode == OperatingMode.STANDBY.value:
            return await self.stop()
        if mode == OperatingMode.AUTO.value:
            return await self.start()
        return True
