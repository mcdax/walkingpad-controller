"""WiLink speed conversion (#21). ph4-walkingpad is optional, so no __init__."""

from __future__ import annotations

import asyncio

import pytest

from walkingpad_controller.wilink import WiLinkController


class FakePh4:
    def __init__(self) -> None:
        self.speeds: list[int] = []

    async def change_speed(self, tenths: int) -> None:
        self.speeds.append(tenths)


def wilink() -> tuple[WiLinkController, FakePh4]:
    c = WiLinkController.__new__(WiLinkController)
    c._controller = FakePh4()  # type: ignore[attr-defined]
    return c, c._controller  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("kmh", "tenths"),
    [(0.8, 8), (0.6 + 0.3, 9), (2.3, 23), (100.0, 60), (0.0, 5)],
)
def test_speed_is_rounded_and_clamped(kmh, tenths) -> None:
    c, ph4 = wilink()
    assert asyncio.run(c.set_target_speed(kmh)) is True
    assert ph4.speeds == [tenths]
