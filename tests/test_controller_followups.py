"""WalkingPadController connect/set_speed fixes and enum behaviour (#19)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from walkingpad_controller import FTMSController, OperatingMode, WalkingPadController
from walkingpad_controller import controller as controller_mod
from walkingpad_controller import ftms as ftms_mod

from test_ftms_stop import Pad, treadmill_frame
from walkingpad_controller.const import FTMSOpcode

DEVICE = SimpleNamespace(name="KS-HD-Z1D", address="AA:BB:CC:DD:EE:FF")


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.disconnects = 0

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False


def test_failed_ftms_setup_closes_the_link(monkeypatch) -> None:
    client = FakeClient()

    async def establish_connection(*args, **kwargs):
        return client

    async def noop(*args, **kwargs):
        return None

    async def boom(*args, **kwargs):
        raise BleakError("CCCD write failed")

    monkeypatch.setattr(ftms_mod, "establish_connection", establish_connection)
    c = FTMSController()
    monkeypatch.setattr(c, "_discover_services", noop)
    monkeypatch.setattr(c, "_read_capabilities", boom)
    with pytest.raises(BleakError):
        asyncio.run(c.connect(DEVICE))  # type: ignore[arg-type]
    assert client.disconnects == 1
    assert not c.connected


def test_failed_attempt_cannot_report_a_disconnect_later(monkeypatch) -> None:
    attempts: list[FTMSController] = []

    async def connect(self, ble_device, name=None):
        attempts.append(self)
        self._client = FakeClient()
        self._connected = True
        if len(attempts) == 1:
            raise BleakError("setup failed")

    monkeypatch.setattr(FTMSController, "connect", connect)
    monkeypatch.setattr(controller_mod, "RETRY_DELAY_SECONDS", 0)
    wp = WalkingPadController(DEVICE)  # type: ignore[arg-type]
    lost: list[bool] = []
    wp.register_disconnect_callback(lambda: lost.append(True))
    asyncio.run(wp.connect())
    assert len(attempts) == 2 and wp.connected
    attempts[0]._on_disconnect(None)  # zombie link from attempt 1 drops
    assert lost == [] and wp.connected


def test_connect_reconnects_when_the_cached_flag_is_stale(monkeypatch) -> None:
    connects: list[FTMSController] = []

    async def connect(self, ble_device, name=None):
        connects.append(self)
        self._client = FakeClient()
        self._connected = True

    monkeypatch.setattr(FTMSController, "connect", connect)
    wp = WalkingPadController(DEVICE)  # type: ignore[arg-type]
    asyncio.run(wp.connect())
    wp._ftms._client.is_connected = False  # link dropped, no callback  # type: ignore[union-attr]
    assert wp._connected and not wp.connected
    asyncio.run(wp.connect())
    assert len(connects) == 2 and wp.connected


def _controller_with(pad: Pad) -> WalkingPadController:
    wp = WalkingPadController(DEVICE)  # type: ignore[arg-type]
    wp._ftms = pad.c
    return wp


def test_set_speed_zero_does_not_start_a_stopped_belt() -> None:
    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.on_pause = []
    assert asyncio.run(_controller_with(pad).set_speed(0)) is True
    assert FTMSOpcode.START_OR_RESUME not in [op for op, _ in pad.sent]


def test_set_speed_zero_pauses_a_running_belt() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)
    pad.on_pause = []
    assert asyncio.run(_controller_with(pad).set_speed(0)) is True
    assert [op for op, _ in pad.sent] == [FTMSOpcode.STOP_OR_PAUSE]


def test_set_speed_while_decelerating_after_pause_takes_the_start_path() -> None:
    pad = Pad(pause_before_stop=False, speed=2.0)
    pad.c._paused = True

    async def run() -> bool:
        asyncio.get_running_loop().call_later(
            0.05, pad.c._on_treadmill_data, 0, treadmill_frame(1.0)
        )
        return await _controller_with(pad).set_speed(3.0)

    asyncio.run(run())
    assert pad.sent[0][0] == FTMSOpcode.START_OR_RESUME


def test_operating_mode_compares_with_the_int_status_mode() -> None:
    assert OperatingMode.MANUAL == 1
    assert OperatingMode(1) is OperatingMode.MANUAL


def test_closed_backend_cannot_report_a_disconnect(monkeypatch) -> None:
    connects: list[FTMSController] = []

    async def connect(self, ble_device, name=None):
        connects.append(self)
        self._client = FakeClient()
        self._connected = True

    monkeypatch.setattr(FTMSController, "connect", connect)
    wp = WalkingPadController(DEVICE)  # type: ignore[arg-type]
    asyncio.run(wp.connect())
    wp._ftms._client.is_connected = False  # type: ignore[union-attr]
    asyncio.run(wp.connect())  # stale flag: closes the old backend
    lost: list[bool] = []
    wp.register_disconnect_callback(lambda: lost.append(True))
    connects[0]._on_disconnect(None)  # Bleak reports the old link closing
    assert lost == [] and wp.connected


def test_set_speed_zero_does_not_claim_a_halt_after_a_disconnect() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)
    pad.on_pause = []
    wp = _controller_with(pad)

    async def pause_and_drop() -> bool:
        pad.drop_link()
        return False

    pad.c.pause = pause_and_drop  # type: ignore[method-assign]
    assert asyncio.run(wp.set_speed(0)) is False
