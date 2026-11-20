"""stop() must pause first while the belt is moving.

KingSmith R3 (KS-AP-RQ3) firmware rejects STOP (OPERATION_FAILED) while the
belt runs and keeps running; KS Fit always pauses, waits for 0 km/h, then stops.
"""

import asyncio
from types import SimpleNamespace

from walkingpad_controller import FTMSController
from walkingpad_controller.const import FTMSOpcode, FTMSStopPauseParam


def _controller(speed: float) -> tuple[FTMSController, list[tuple[int, bytes]]]:
    c = FTMSController()
    c._has_control = True
    c._connected = True
    c._client = SimpleNamespace(is_connected=True)  # type: ignore[assignment]
    c._status.speed = speed
    sent: list[tuple[int, bytes]] = []

    async def fake_write(opcode, params=b"", *args, **kwargs):
        sent.append((opcode, bytes(params)))
        if opcode == FTMSOpcode.STOP_OR_PAUSE and bytes(params) == bytes([FTMSStopPauseParam.PAUSE]):
            c._status.speed = 0.0  # belt ramps down after PAUSE
        return True

    c._write_control_point = fake_write  # type: ignore[method-assign]
    return c, sent


def test_stop_while_moving_pauses_first() -> None:
    c, sent = _controller(speed=3.0)
    assert asyncio.run(c.stop()) is True
    assert sent == [
        (FTMSOpcode.STOP_OR_PAUSE, bytes([FTMSStopPauseParam.PAUSE])),
        (FTMSOpcode.STOP_OR_PAUSE, bytes([FTMSStopPauseParam.STOP])),
    ]


def test_stop_when_idle_sends_stop_only() -> None:
    c, sent = _controller(speed=0.0)
    assert asyncio.run(c.stop()) is True
    assert sent == [(FTMSOpcode.STOP_OR_PAUSE, bytes([FTMSStopPauseParam.STOP]))]
