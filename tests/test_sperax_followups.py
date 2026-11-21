"""Sperax / WLT6200 fixes: poll loop give-up, incline, targets, decode (#20)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from walkingpad_controller import sperax as sperax_mod
from walkingpad_controller.const import BeltState
from walkingpad_controller.sperax import SperaxController, decode, encode


def status(speed_tenths: int, state: int = 0x10, incline: int = 0) -> bytes:
    return bytes(
        [0x00, 0x19, 0, 0, state, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, speed_tenths, incline, 0, 0]
    )


def offline(ctrl: SperaxController) -> list[bytes]:
    sent: list[bytes] = []

    async def write(inner):
        sent.append(bytes(inner))

    ctrl._write = write  # type: ignore[method-assign]
    return sent


def test_decode_does_not_destuff_the_len_byte() -> None:
    frame = bytearray(encode(bytes([0x00, 0x15, 0x01, 0x1E, 0x00])))
    frame[1] = 0xF0  # LEN is never stuffed; a 0xF0 there must not eat a byte
    assert decode(bytes(frame)) == bytes([0x00, 0x15, 0x01, 0x1E, 0x00])


def test_poll_loop_gives_up_the_link_and_reports_it(monkeypatch) -> None:
    monkeypatch.setattr(sperax_mod, "_POLL_INTERVAL", 0)
    ctrl = SperaxController()

    async def write_gatt_char(*args, **kwargs):
        raise TimeoutError

    async def disconnect():
        client.is_connected = False

    client = SimpleNamespace(
        is_connected=True, write_gatt_char=write_gatt_char, disconnect=disconnect
    )
    ctrl._client = client  # type: ignore[assignment]
    ctrl._connected = True
    lost: list[bool] = []
    ctrl.register_disconnect_callback(lambda: lost.append(True))

    asyncio.run(asyncio.wait_for(ctrl._poll_loop(), 1.0))
    assert not ctrl.connected
    assert lost == [True]
    assert client.is_connected is False


def test_incline_after_pause_does_not_restart_the_belt() -> None:
    ctrl = SperaxController()
    sent = offline(ctrl)
    ctrl._parse_status(status(30))  # running at 3.0 km/h

    async def run() -> None:
        await ctrl.pause()
        ctrl._parse_status(status(20, state=0x0F))  # still decelerating
        await ctrl.set_target_inclination(3)

    asyncio.run(run())
    assert [s[2] for s in sent] == [0x02]  # only the pause frame
    assert ctrl._target_incline == 3


def test_incline_while_running_is_applied() -> None:
    ctrl = SperaxController()
    sent = offline(ctrl)
    ctrl._parse_status(status(30))
    asyncio.run(ctrl.set_target_inclination(2))
    assert sent == [bytes([0x00, 0x15, 0x01, 30, 2])]


def test_connecting_to_a_stopped_belt_keeps_the_minimum_target() -> None:
    ctrl = SperaxController()
    sent = offline(ctrl)
    ctrl._parse_status(status(0, state=0x01))  # first frame: stopped
    assert ctrl._target_speed_tenths == round(ctrl.min_speed * 10)
    # belt started from the remote, then an incline change
    ctrl._parse_status(status(25))
    asyncio.run(ctrl.set_target_inclination(1))
    assert sent == [bytes([0x00, 0x15, 0x01, 25, 1])]  # keeps the remote's speed


def test_idle_device_resets_the_start_speed() -> None:
    ctrl = SperaxController()
    offline(ctrl)
    ctrl._parse_status(status(60))
    ctrl._parse_status(status(0, state=0x00))  # session ended on the device
    assert ctrl._target_speed_tenths == round(ctrl.min_speed * 10)


def test_paused_belt_reads_paused() -> None:
    ctrl = SperaxController()
    offline(ctrl)
    ctrl._parse_status(status(30))
    asyncio.run(ctrl.pause())
    ctrl._parse_status(status(0, state=0x01))
    assert ctrl.status.belt_state == BeltState.PAUSED
    asyncio.run(ctrl.stop())
    ctrl._parse_status(status(0, state=0x01))
    assert ctrl.status.belt_state == BeltState.STOPPED


def test_explicit_speed_is_not_overridden_while_ramping() -> None:
    ctrl = SperaxController()
    offline(ctrl)
    ctrl._parse_status(status(10))
    asyncio.run(ctrl.set_target_speed(5.0))
    ctrl._parse_status(status(22))  # ramping up towards 5.0
    assert ctrl._target_speed_tenths == 50


def test_pausing_an_idle_belt_does_not_read_paused() -> None:
    ctrl = SperaxController()
    offline(ctrl)
    ctrl._parse_status(status(0, state=0x01))
    asyncio.run(ctrl.pause())
    ctrl._parse_status(status(0, state=0x01))
    assert ctrl.status.belt_state == BeltState.STOPPED


def test_remote_resume_and_device_idle_clear_paused() -> None:
    ctrl = SperaxController()
    offline(ctrl)
    ctrl._parse_status(status(30))
    asyncio.run(ctrl.pause())
    ctrl._parse_status(status(25))  # resumed from the remote
    ctrl._parse_status(status(0, state=0x01))
    assert ctrl.status.belt_state == BeltState.STOPPED


def test_poll_give_up_and_bleak_callback_report_once(monkeypatch) -> None:
    monkeypatch.setattr(sperax_mod, "_POLL_INTERVAL", 0)
    ctrl = SperaxController()

    async def write_gatt_char(*args, **kwargs):
        raise TimeoutError

    async def disconnect():
        client.is_connected = False
        ctrl._on_disconnect(client)  # Bleak fires its callback as well

    client = SimpleNamespace(
        is_connected=True, write_gatt_char=write_gatt_char, disconnect=disconnect
    )
    ctrl._client = client  # type: ignore[assignment]
    ctrl._connected = True
    lost: list[bool] = []
    ctrl.register_disconnect_callback(lambda: lost.append(True))
    asyncio.run(asyncio.wait_for(ctrl._poll_loop(), 1.0))
    assert lost == [True]


def test_pausing_twice_keeps_the_paused_session() -> None:
    ctrl = SperaxController()
    offline(ctrl)
    ctrl._parse_status(status(30))
    asyncio.run(ctrl.pause())
    ctrl._parse_status(status(0, state=0x01))
    asyncio.run(ctrl.pause())  # e.g. speed slider moved to 0 again
    ctrl._parse_status(status(0, state=0x01))
    assert ctrl.status.belt_state == BeltState.PAUSED


def test_incline_during_the_pause_write_does_not_restart_the_belt() -> None:
    ctrl = SperaxController()
    sent: list[bytes] = []
    ctrl._parse_status(status(30))

    async def slow_write(inner):
        await asyncio.sleep(0.05)
        sent.append(bytes(inner))

    ctrl._write = slow_write  # type: ignore[method-assign]

    async def run() -> None:
        pause = asyncio.create_task(ctrl.pause())
        await asyncio.sleep(0.01)  # pause write in flight
        await ctrl.set_target_inclination(4)
        await pause

    asyncio.run(run())
    assert [s[2] for s in sent] == [0x02]
