"""Buzzer control over the supplement channel (frames from a KS-HD-Z1D)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from walkingpad_controller import FTMSController, ftms, supplement

PROPS_OFF = "72801c0100000004000300050000000200 3c40080001000a00000206000000b4"
PROPS_ON = "72801c0100000004000300050000000200 3c40080003000a00000206000000b6"


def h(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.mark.parametrize(
    ("name", "nonce", "expected"),
    [
        ("KS-AP-RQ3", 0x87, "71000587b452513387"),
        ("KS-AP-RQ3", 0xFA, "710005fa275351336e"),
        ("KS-HD-Z1D", 0x1F, "7100051f4c5a3144b0"),
        ("KS-HD-Z1D", 0xA6, "710005a6d35a3144be"),
    ],
)
def test_unlock_frame_matches_captures(name, nonce, expected) -> None:
    assert supplement.unlock_frame(name, nonce) == h(expected)


def test_property_frames_match_captures() -> None:
    assert supplement.set_property_frame(8, 3) == h("72010308030081")
    assert supplement.set_property_frame(8, 1) == h("7201030801007f")
    assert supplement.parse_properties(h(PROPS_OFF))[8] == 1
    assert supplement.parse_properties(h(PROPS_ON))[8] == 3


@pytest.mark.parametrize("data", ["", "7280", PROPS_OFF[:-2] + "00", "72800201000000"])
def test_bad_property_reply(data) -> None:
    with pytest.raises(ValueError):
        supplement.parse_properties(h(data))


class FakePad:
    is_connected = True

    def __init__(self, value=1, silent=False, refuse=False):
        self.value, self.silent, self.refuse = value, silent, refuse
        self.written: list[bytes] = []
        self.subscribed = 0
        self.handler = None

    async def start_notify(self, uuid, handler):
        self.subscribed += 1
        self.handler = handler

    async def stop_notify(self, uuid):
        self.subscribed -= 1

    async def write_gatt_char(self, uuid, data, response):
        self.written.append(bytes(data))
        if self.silent:
            return
        if data[0] == 0x71:
            reply = h("718000f1")
        elif data[1] == 0:
            reply = h(PROPS_OFF)
            reply = bytearray(reply)
            at = reply.index(b"\x08\x00\x01\x00") + 2
            reply[at] = self.value
            reply[-1] = sum(reply[:-1]) & 0xFF
        else:
            if not self.refuse:
                self.value = data[4]
            reply = h("7281020800fd")
        self.handler(0, bytearray(reply))


def controller(pad, name="KS-HD-Z1D") -> FTMSController:
    c = FTMSController()
    c._client = pad  # type: ignore[assignment]
    c._connected = True
    c._ble_name = name
    c._capabilities.has_supplement = True
    return c


@pytest.mark.parametrize(("start", "on", "end"), [(1, True, 3), (3, False, 1), (0x0D, True, 0x0F)])
def test_set_sound_changes_only_the_sound_bit(start, on, end) -> None:
    pad = FakePad(start)
    assert asyncio.run(controller(pad).set_sound(on)) is True
    assert pad.value == end


def test_unlocks_once_per_connection() -> None:
    pad = FakePad()
    c = controller(pad)

    async def run():
        await c.set_sound(True)
        await c.set_sound(False)
        c._reset_link_state()
        c._connected = True
        await c.set_sound(True)

    asyncio.run(run())
    assert [f[0] for f in pad.written].count(0x71) == 2


def test_already_set_writes_nothing() -> None:
    pad = FakePad(1)
    assert asyncio.run(controller(pad).set_sound(False)) is True
    assert not any(f[:2] == h("7201") for f in pad.written)


def test_refused_set_fails() -> None:
    assert asyncio.run(controller(FakePad(refuse=True)).set_sound(True)) is False


def test_no_reply_fails_and_can_be_retried(monkeypatch) -> None:
    monkeypatch.setattr(ftms, "_SUPPLEMENT_TIMEOUT", 0.01)
    pad = FakePad(silent=True)
    c = controller(pad)

    async def run():
        assert await c.set_sound(True) is False
        assert pad.subscribed == 1
        pad.silent = False
        assert await c.set_sound(True) is True

    asyncio.run(run())


def test_link_loss_ends_the_wait() -> None:
    pad = FakePad(silent=True)
    c = controller(pad)

    async def run():
        task = asyncio.create_task(c.set_sound(True))
        await asyncio.sleep(0.01)
        c._reset_link_state()
        return await asyncio.wait_for(task, 0.5)

    assert asyncio.run(run()) is False


@pytest.mark.parametrize("name", ["KS-AP-RQ3", "KS-MC21-D06BFD", ""])
def test_unsupported_devices_are_left_alone(name) -> None:
    pad = FakePad()
    assert asyncio.run(controller(pad, name).set_sound(True)) is False
    assert pad.written == []


def test_controller_knows_support_from_the_name() -> None:
    from walkingpad_controller import WalkingPadController

    def make(ble, name):
        return WalkingPadController(ble_device=SimpleNamespace(name=ble, address="AA"), name=name)

    assert make("KS-HD-Z1D", None).supports_sound
    assert make(None, "KS-HD-Z1D").supports_sound
    assert not make("KS-AP-RQ3", None).supports_sound


def test_drop_right_after_the_unlock_reply_is_not_remembered() -> None:
    pad = FakePad()
    c = controller(pad)
    inner = pad.write_gatt_char

    async def write(uuid, data, response):
        await inner(uuid, data, response)
        if data[0] == 0x71:
            c._reset_link_state()

    pad.write_gatt_char = write  # type: ignore[method-assign]
    assert asyncio.run(c.set_sound(True)) is False
    assert c._supplement_unlocked is False


def _scripted(pad, monkeypatch, drop):
    """Drop the replies to the n-th writes listed in ``drop``."""
    monkeypatch.setattr(ftms, "_SUPPLEMENT_TIMEOUT", 0.01)
    inner, count = pad.write_gatt_char, [0]

    async def write(uuid, data, response):
        count[0] += 1
        pad.silent = count[0] in drop
        await inner(uuid, data, response)
        if pad.silent and data[:2] == h("7201"):
            pad.value = data[4]  # applied, only the reply is lost

    pad.write_gatt_char = write


def test_relocked_channel_is_unlocked_again(monkeypatch) -> None:
    pad = FakePad(1)
    _scripted(pad, monkeypatch, {5})  # 1 unlock, 2 read, 3 set, 4 read, 5 read
    c = controller(pad)

    async def run():
        assert await c.set_sound(True)
        return await c.set_sound(False)

    assert asyncio.run(run()) is True
    assert [f[0] for f in pad.written].count(0x71) == 2
    assert pad.subscribed == 1


def test_acknowledged_set_counts_without_readback(monkeypatch) -> None:
    pad = FakePad(1)
    _scripted(pad, monkeypatch, {4, 5, 6})
    assert asyncio.run(controller(pad).set_sound(True)) is True
    assert pad.value == 3


def test_refusal_status_fails_at_once() -> None:
    pad = FakePad(1)
    inner = pad.write_gatt_char

    async def write(uuid, data, response):
        if data[:2] == h("7201"):
            pad.handler(0, bytearray(h("728102070702")))
            return
        await inner(uuid, data, response)

    pad.write_gatt_char = write
    assert asyncio.run(controller(pad).set_sound(True)) is False
