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
        state 0x01=run / 0x02=pause (keep counters) / 0x00=stop (reset counters);
        speed = km/h x 10 ; incline 0..10
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

# Run-command state byte (2nd arg of 0x15). Confirmed in hass-walkingpad#3:
#   0x01 = run, 0x02 = pause (belt stops, session counters kept),
#   0x00 = stop (belt stops AND steps/distance/time reset to 0).
_RUN_STATE_RUN = 0x01
_RUN_STATE_PAUSE = 0x02
_RUN_STATE_STOP = 0x00
_VIB_STATE_ON = 0x01
_VIB_STATE_OFF = 0x00

# Status-frame state byte (offset 4). 0x50 means the device is in vibration
# mode; the vibration-level field (offset 18) retains its last value even
# after vibration is turned off, so it's only meaningful in this state.
_STATUS_STATE_VIBRATION = 0x50

# How often to poll the device. The WLT6200 only streams status while it is
# being polled; the official app polls ~3x/s. 0.5 s keeps the link alive and
# the status fresh without hammering the write characteristic.
_POLL_INTERVAL = 0.5

# Consecutive failed poll writes tolerated before giving up the poll loop.
# Absorbs the occasional dropped packet on a marginal BT-proxy link instead
# of tearing down on the first hiccup.
_MAX_POLL_FAILURES = 3

# Speed capabilities. The P3 Max tops out at 12.0 km/h (confirmed by the
# device owner in hass-walkingpad#3). The device is not known to expose a
# readable speed-range characteristic, so these are fixed defaults.
_MIN_SPEED = 0.5
_MAX_SPEED = 12.0
_SPEED_INCREMENT = 0.1
_MAX_INCLINE = 10
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

        # The device is the source of truth: on (re)connect we adopt its actual
        # speed and incline from the first status frame, rather than assuming
        # the defaults above. A new controller is created on every connect
        # (including reconnect after a dropped link), so this re-syncs state
        # each time and avoids, e.g., an incline nudge re-sending a stale
        # minimum speed. Cleared on disconnect; set once per connection.
        self._synced_from_device = False

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
        self._synced_from_device = False
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
        """Periodically poll the device so it keeps streaming status.

        A single failed poll write is tolerated: over an ESPHome BT proxy (or
        any marginal link) the odd dropped packet is normal, and tearing the
        loop down on the first error turns one hiccup into a full
        disconnect/reconnect cycle. We only give up after
        ``_MAX_POLL_FAILURES`` consecutive failures; a success resets the
        counter. If the link is genuinely gone, ``self.connected`` goes false
        and the loop exits on its own.
        """
        failures = 0
        try:
            while self.connected:
                try:
                    await self._write(bytes([0x00, _CMD_STATUS]))
                    failures = 0
                except BleakError as err:
                    failures += 1
                    _LOGGER.debug(
                        "Sperax: poll write failed (%d/%d): %s",
                        failures,
                        _MAX_POLL_FAILURES,
                        err,
                    )
                    if failures >= _MAX_POLL_FAILURES:
                        _LOGGER.warning(
                            "Sperax: stopping poll after %d consecutive failures",
                            failures,
                        )
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
          4      state (0x10 run, 0x0F decel, 0x01 stopped, 0x50 vibration, 0x00 idle)
          8-9    duration, seconds (uint16 LE)
          10-11  distance, in 10 m units (uint16 LE) — so metres = value * 10
          12-13  calorie-like counter (uint16 LE); left unmapped (the kcal the
                 app shows is computed app-side and only coarsely broadcast)
          14     steps (uint8)
          15     speed x10   16 incline   18 vibration level

        Distance/duration decoded against a real walk: integrating speed over
        the session gives ~30 m, and offset 10 reaches 3 (x10 = 30 m); offset 8
        reaches the elapsed-second count. Steps were confirmed on-device by the
        owner (hass-walkingpad#3).
        """
        if len(inner) < 19:
            return

        speed_raw = inner[15]
        self._status.speed = speed_raw / 10.0
        if speed_raw > 0:
            self._status.belt_state = BeltState.ACTIVE
        else:
            self._status.belt_state = BeltState.STOPPED

        # The vibration-level field (offset 18) holds the *last selected*
        # level and is not cleared when vibration turns off — the device
        # signals "vibration active" via the state byte (offset 4 == 0x50).
        # Report a level only while actually vibrating, else 0.
        vib = inner[18] if inner[4] == _STATUS_STATE_VIBRATION else 0
        self._vibration_level = vib
        self._status.vibration_level = vib
        self._status.incline = inner[16]

        # Adopt the device's actual speed/incline as our run targets on the
        # first status frame after (re)connecting — the device is the source
        # of truth. Without this, the freshly-created controller would keep its
        # default targets (min speed, flat), so the next run command (e.g. from
        # an incline nudge) would wrongly slow the belt to minimum.
        if not self._synced_from_device:
            self._target_speed_tenths = speed_raw
            self._target_incline = inner[16]
            self._synced_from_device = True

        # Session counters.
        self._status.duration = inner[8] | (inner[9] << 8)  # seconds
        self._status.distance = (inner[10] | (inner[11] << 8)) * 10  # metres (10 m units)
        self._status.steps = inner[14]

        self._status.timestamp = time.time()
        self._notify_status()

    # --- Commands ---

    async def _send_run(self) -> bool:
        """(Re)send the current run target (speed + incline)."""
        # We now have explicit intent; don't let a passive status frame
        # overwrite these targets via the on-connect device sync.
        self._synced_from_device = True
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
        """Stop the belt and end the session (``15 00 00 00``).

        This is the full stop: the belt stops AND the device resets its
        session counters (steps / distance / time) to 0 — matching the FTMS
        backend's stop() semantics. Use pause() to stop the belt while keeping
        the running totals.

        A stop is a full reset, so the cached run targets are cleared too: the
        next start() begins flat (incline 0) and at the minimum speed rather
        than resuming the previous run. (The incline byte in a *run* frame does
        drive the bed, so the next start actually levels it — the stop frame
        itself carries incline 0 but the device does not level from a stop.)
        """
        try:
            await self._write(bytes([0x00, _CMD_RUN, _RUN_STATE_STOP, 0x00, 0x00]))
            self._target_speed_tenths = int(round(self.min_speed * 10))
            self._target_incline = 0
            self._synced_from_device = True
            return True
        except BleakError as err:
            _LOGGER.warning("Sperax: stop failed: %s", err)
            return False

    async def pause(self) -> bool:
        """Pause the belt, keeping the session (``15 02 00 00``).

        The belt stops but the device preserves its session counters
        (steps / distance / time), and the cached run targets (speed + incline)
        are left intact, so a subsequent start() resumes the previous run —
        matching the FTMS backend's pause() semantics.
        """
        try:
            await self._write(bytes([0x00, _CMD_RUN, _RUN_STATE_PAUSE, 0x00, 0x00]))
            self._synced_from_device = True
            return True
        except BleakError as err:
            _LOGGER.warning("Sperax: pause failed: %s", err)
            return False

    async def set_target_speed(self, speed_kmh: float) -> bool:
        """Set the belt speed in km/h."""
        speed_kmh = max(self.min_speed, min(self.max_speed, speed_kmh))
        self._target_speed_tenths = int(round(speed_kmh * 10))
        return await self._send_run()

    async def set_target_inclination(self, incline_step: int) -> bool:
        """Set the incline as a step (0 = flat .. 10 = max).

        Unlike FTMS (which uses a percentage), the P3 Max exposes incline as
        discrete steps, clamped to 0..10.

        Incline rides inside the run command (``15 01 <speed> <incline>``), so
        applying it re-sends a run. We only do that while the belt is already
        moving — sending it while stopped would start the belt. When stopped we
        just cache the target; it is applied on the next start()/set_speed().
        """
        self._target_incline = max(0, min(_MAX_INCLINE, int(incline_step)))
        self._synced_from_device = True
        if self._status.speed > 0:
            return await self._send_run()
        return True

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
