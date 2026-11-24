"""A paused session ending while no Treadmill Data arrives."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from walkingpad_controller import FTMSController
from walkingpad_controller.const import BeltState
from walkingpad_controller.ftms import _CPResult


def paused() -> tuple[FTMSController, list]:
    c = FTMSController()
    c._client = SimpleNamespace(is_connected=True)  # type: ignore[assignment]
    c._connected = c._has_control = c._paused = True
    c._machine_status_first_event_seen = True
    s = c._status
    s.belt_state, s.duration, s.distance, s.steps, s.calories = BeltState.PAUSED, 184, 50, 81, 3
    accepted: list = []

    def on_status(status):  # like the HA coordinator: only newer updates
        if not accepted or status.timestamp > accepted[-1][0]:
            accepted.append(
                (status.timestamp, status.belt_state, status.duration, status.steps)
            )

    c.register_status_callback(on_status)
    return c, accepted


@pytest.mark.parametrize("event", [None, b"\x01", b"\x02\x01"])
def test_stop_from_pause_reaches_the_consumer(event) -> None:
    c, accepted = paused()

    async def write(*args, **kwargs):
        if event:
            c._on_machine_status(0, bytearray(event))
        return _CPResult.ACK

    c._write_control_point_result = write  # type: ignore[method-assign]
    assert asyncio.run(c.stop()) is True
    assert accepted[-1][1:] == (BeltState.STOPPED, 0, 0)


def test_idle_reset_ends_a_paused_session() -> None:
    c, accepted = paused()
    c._on_machine_status(0, bytearray(b"\x01"))
    assert accepted[-1][1:] == (BeltState.STOPPED, 0, 0)


def test_reset_while_moving_changes_nothing() -> None:
    c, _ = paused()
    c._paused = False
    c._status.speed, c._status.belt_state = 2.0, BeltState.ACTIVE
    c._on_machine_status(0, bytearray(b"\x01"))
    assert c._status.belt_state == BeltState.ACTIVE and c._status.duration == 184
