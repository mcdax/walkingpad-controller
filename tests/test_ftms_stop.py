"""FTMSController.stop() on pause-before-stop devices (KingSmith R3).

R3 firmware (KS-AP-RQ3) answers STOP with OPERATION_FAILED while the belt is
moving and keeps running. KS Fit pauses, waits for 0 km/h, then stops. These
tests drive the controller with asynchronously delivered Treadmill Data and
check that STOP is never sent before telemetry received *after* the PAUSE
confirms standstill.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from types import SimpleNamespace

import pytest

from walkingpad_controller import FTMSController
from walkingpad_controller.const import BeltState, FTMSOpcode, FTMSStopPauseParam
from walkingpad_controller.ftms import _CPResult

PAUSE = (FTMSOpcode.STOP_OR_PAUSE, bytes([FTMSStopPauseParam.PAUSE]))
STOP = (FTMSOpcode.STOP_OR_PAUSE, bytes([FTMSStopPauseParam.STOP]))
FRAME_GAP = 0.02  # seconds between simulated Treadmill Data frames


def treadmill_frame(kmh: float) -> bytearray:
    """Minimal Treadmill Data frame: flags 0 (speed only)."""
    return bytearray(struct.pack("<HH", 0, round(kmh * 100)))


class Pad:
    """FTMSController wired to a fake link.

    ``on_pause`` is a list of speeds the belt reports, one frame every
    FRAME_GAP, after each PAUSE is written (``pause_frames``, if set, gives
    one such list per PAUSE). ``stop_frames`` likewise gives the frames
    played after each STOP. ``results`` maps a command to the
    acknowledgement result (a list is consumed one entry per write).
    """

    def __init__(self, *, pause_before_stop: bool, speed: float = 3.0) -> None:
        self.c = FTMSController()
        self.c._has_control = True
        self.c._connected = True
        self.c._client = SimpleNamespace(is_connected=True)  # type: ignore[assignment]
        self.c._capabilities.pause_before_stop = pause_before_stop
        self.c._status.speed = speed
        self.c._standstill_timeout = 0.3
        self.c._countdown_grace = 0.3
        self.c._start_rejected_grace = 0.3
        self.sent: list[tuple[int, bytes]] = []
        # speed in _status at the moment each command was written, and how
        # many frames had been delivered since the last PAUSE
        self.speed_at_send: list[float] = []
        self.frames_since_pause_at_send: list[int] = []
        self.on_pause: list[float] = [2.0, 1.0, 0.0]
        self.pause_frames: list[list[float]] = []
        self.stop_frames: list[list[float]] = []
        self.results: dict[tuple[int, bytes], list[bool]] = {}
        self.session_end_on_stop = False
        self._frames_since_pause = 0
        self._tasks: list[asyncio.Task] = []
        self.c._write_control_point_result = self._write_result  # type: ignore[method-assign]

    async def _write_result(self, opcode, params=b"", *args, **kwargs) -> _CPResult:
        result = await self._write(opcode, params)
        if isinstance(result, _CPResult):
            return result
        return _CPResult.ACK if result else _CPResult.REJECTED

    async def _write(self, opcode, params=b"", *args, **kwargs):
        cmd = (opcode, bytes(params))
        self.sent.append(cmd)
        self.speed_at_send.append(self.c._status.speed)
        self.frames_since_pause_at_send.append(self._frames_since_pause)
        if cmd == PAUSE:
            self._frames_since_pause = 0
            frames = self.pause_frames.pop(0) if self.pause_frames else list(self.on_pause)
            self._tasks.append(asyncio.create_task(self._play(frames)))
        if cmd == STOP:
            if self.session_end_on_stop:
                self.c._on_machine_status(0, bytearray([0x02, 0x01]))
            if self.stop_frames:
                self._tasks.append(asyncio.create_task(self._play(self.stop_frames.pop(0))))
        await asyncio.sleep(0)
        queue = self.results.get(cmd)
        return queue.pop(0) if queue else True

    async def _play(self, speeds: list[float]) -> None:
        for kmh in speeds:
            await asyncio.sleep(FRAME_GAP)
            if not self.c.connected:
                return
            self._frames_since_pause += 1
            self.c._on_treadmill_data(0, treadmill_frame(kmh))

    def drop_link(self) -> None:
        self.c._client.is_connected = False  # type: ignore[union-attr]
        self.c._on_disconnect(None)  # type: ignore[arg-type]

    def stop(self, *, first_status_event_seen: bool = True) -> bool:
        async def run() -> bool:
            # by default, mark the first FM Status event as already seen so
            # the simulated events take the normal path
            self.c._machine_status_first_event_seen = first_status_event_seen
            try:
                return await self.c.stop()
            finally:
                for t in self._tasks:
                    t.cancel()

        return asyncio.run(run())


# --- devices without the quirk keep the plain STOP ------------------------


def test_regular_ftms_device_sends_stop_directly_even_while_moving() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)
    assert pad.stop() is True
    assert pad.sent == [STOP]


def test_regular_ftms_device_reports_rejected_stop() -> None:
    pad = Pad(pause_before_stop=False)
    pad.results[STOP] = [False]
    assert pad.stop() is False
    assert pad.sent == [STOP]


def test_stop_from_pause_reports_stopped_without_session_end_event() -> None:
    # KS-HD-Z1D: STOP while paused is acked, followed by FM Status RESET
    # (0x01) rather than 02 01, and Treadmill Data keeps reporting 0 km/h.
    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.c._paused = True
    pad.c._status.belt_state = BeltState.PAUSED
    seen: list[BeltState] = []
    pad.c.register_status_callback(lambda st: seen.append(st.belt_state))
    assert pad.stop() is True
    assert pad.c._paused is False
    assert pad.c.status.belt_state is BeltState.STOPPED
    assert seen[-1] is BeltState.STOPPED


def test_rejected_stop_from_pause_stays_paused() -> None:
    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.c._paused = True
    pad.c._status.belt_state = BeltState.PAUSED
    pad.results[STOP] = [False]
    assert pad.stop() is False
    assert pad.c.status.belt_state is BeltState.PAUSED


# --- pause-before-stop devices -------------------------------------------


def test_pauses_then_stops_only_after_fresh_zero_speed() -> None:
    pad = Pad(pause_before_stop=True, speed=3.0)
    assert pad.stop() is True
    assert pad.sent == [PAUSE, STOP]
    # STOP went out only after all three deceleration frames, the last at 0
    assert pad.frames_since_pause_at_send[1] == 3
    assert pad.speed_at_send[1] == 0.0


@pytest.mark.parametrize("cached", [0.0, 3.0])
def test_cached_speed_is_not_trusted(cached: float) -> None:
    # A cached 0 may be stale (e.g. from before a reconnect) or lag a
    # remote start; PAUSE is sent and fresh telemetry is awaited either way.
    pad = Pad(pause_before_stop=True, speed=cached)
    pad.on_pause = [1.0, 0.0]
    assert pad.stop() is True
    assert pad.sent == [PAUSE, STOP]
    assert pad.frames_since_pause_at_send[1] == 2


def test_cached_zero_without_any_new_frame_does_not_send_stop(caplog) -> None:
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.on_pause = []
    with caplog.at_level(logging.WARNING):
        assert pad.stop() is False
    assert pad.sent == [PAUSE]
    assert "no Treadmill Data" in caplog.text


def test_belt_still_moving_at_timeout_does_not_send_stop(caplog) -> None:
    pad = Pad(pause_before_stop=True)
    pad.on_pause = [3.0] * 30  # keeps reporting motion past the timeout
    with caplog.at_level(logging.WARNING):
        assert pad.stop() is False
    assert STOP not in pad.sent
    assert "still reports 3.0 km/h" in caplog.text


def test_rejected_pause_with_belt_still_moving_does_not_send_stop() -> None:
    pad = Pad(pause_before_stop=True)
    pad.results[PAUSE] = [False]
    pad.on_pause = [3.0] * 30
    assert pad.stop() is False
    assert pad.sent == [PAUSE]


def test_unacknowledged_pause_but_belt_stops_still_ends_session() -> None:
    # e.g. the PAUSE indication was dropped, or the remote paused already
    pad = Pad(pause_before_stop=True)
    pad.results[PAUSE] = [False]
    assert pad.stop() is True
    assert pad.sent == [PAUSE, STOP]
    assert pad.speed_at_send[1] == 0.0


def test_disconnect_during_deceleration(caplog) -> None:
    pad = Pad(pause_before_stop=True)
    pad.on_pause = [2.5, 2.0, 1.5, 1.0, 0.5, 0.0]

    async def run() -> bool:
        task = asyncio.create_task(pad.c.stop())
        await asyncio.sleep(FRAME_GAP * 2.5)
        pad.drop_link()
        return await task

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) is False
    assert STOP not in pad.sent
    assert "connection lost" in caplog.text
    assert "belt state unknown" in caplog.text


def test_no_session_pause_and_stop_rejected_returns_false(caplog) -> None:
    # Verified on the R3: with no session, PAUSE -> 80 08 04 02 and
    # STOP -> 80 08 04 01, and the belt stays at rest.
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.on_pause = [0.0, 0.0]
    pad.results[PAUSE] = [False]
    pad.results[STOP] = [False]
    with caplog.at_level(logging.WARNING):
        assert pad.stop() is False
    assert pad.sent == [PAUSE, STOP]
    assert "stayed at rest" in caplog.text


def test_stop_during_start_countdown_pauses_once_belt_moves() -> None:
    # Verified on the R3: during the 3-2-1 countdown both PAUSE and STOP are
    # rejected at 0 km/h and the belt then starts at 1.0 km/h regardless.
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.results[PAUSE] = [False, True]
    pad.results[STOP] = [False, True]
    pad.pause_frames = [[0.0], [0.5, 0.0]]  # countdown frame; then ramp-down
    pad.stop_frames = [[0.0, 1.0, 1.0]]  # countdown ends, belt starts
    assert pad.stop() is True
    assert pad.sent == [PAUSE, STOP, PAUSE, STOP]
    assert pad.speed_at_send[2] == 1.0  # second PAUSE once the belt moved
    assert pad.speed_at_send[3] == 0.0  # final STOP only at fresh standstill


def test_stop_rejected_again_after_second_pause_gives_up(caplog) -> None:
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.results[PAUSE] = [False, True]
    pad.results[STOP] = [False, False]
    pad.pause_frames = [[0.0], [0.5, 0.0]]
    pad.stop_frames = [[1.0]]
    with caplog.at_level(logging.WARNING):
        assert pad.stop() is False
    assert pad.sent == [PAUSE, STOP, PAUSE, STOP]
    assert "session not ended" in caplog.text


def test_disconnect_while_watching_for_countdown_start(caplog) -> None:
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.on_pause = [0.0]
    pad.results[STOP] = [False]

    async def run() -> bool:
        task = asyncio.create_task(pad.c.stop())
        await asyncio.sleep(FRAME_GAP * 3)
        pad.drop_link()
        return await task

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) is False
    assert pad.sent == [PAUSE, STOP]
    assert "connection lost" in caplog.text


def test_dropped_stop_indication_but_session_end_event_counts() -> None:
    pad = Pad(pause_before_stop=True)
    pad.results[STOP] = [_CPResult.TIMEOUT]  # type: ignore[list-item]
    pad.session_end_on_stop = True
    assert pad.stop() is True
    assert pad.sent == [PAUSE, STOP]


# --- serialisation ---------------------------------------------------------


def test_set_speed_waits_for_the_whole_stop_sequence() -> None:
    pad = Pad(pause_before_stop=True)

    async def run() -> None:
        stop = asyncio.create_task(pad.c.stop())
        await asyncio.sleep(FRAME_GAP / 2)  # stop() has sent PAUSE, is waiting
        speed = asyncio.create_task(pad.c.set_target_speed(2.0))
        assert await stop is True
        assert await speed is True

    asyncio.run(run())
    assert [op for op, _ in pad.sent] == [
        FTMSOpcode.STOP_OR_PAUSE,
        FTMSOpcode.STOP_OR_PAUSE,
        FTMSOpcode.SET_TARGET_SPEED,
    ]
    assert pad.sent[:2] == [PAUSE, STOP]


def test_control_point_acks_are_not_shared_between_concurrent_writes() -> None:
    """Two commands in flight must each get their own indication."""
    c = FTMSController()
    c._connected = True
    c._has_control = True
    writes: list[bytes] = []

    async def write_gatt_char(uuid, data, response=True):
        writes.append(bytes(data))
        # indication arrives a little later; PAUSE is rejected, speed accepted
        result = 0x04 if bytes(data) == bytes([0x08, 0x02]) else 0x01
        asyncio.get_running_loop().call_later(
            0.02, c._on_control_point_response, 0, bytearray([0x80, data[0], result])
        )

    c._client = SimpleNamespace(  # type: ignore[assignment]
        is_connected=True, write_gatt_char=write_gatt_char
    )

    async def run():
        return await asyncio.gather(c.pause(), c.set_target_speed(2.0))

    paused, speed = asyncio.run(run())
    assert paused is False and speed is True
    assert [w[0] for w in writes] == [0x08, 0x02]


def test_start_waits_for_run_down_after_stop() -> None:
    # KS-HD-Z1D: START/SET_TARGET_SPEED sent while the belt runs down after
    # STOP are acked but lost, so start() must wait for 0 km/h first.
    pad = Pad(pause_before_stop=False, speed=2.5)
    pad.c._machine_status_first_event_seen = True
    pad.c._on_machine_status(0, bytearray([0x02, 0x01]))  # STOP while moving
    assert pad.c._stopping is True

    async def run() -> bool:
        async def run_down() -> None:
            for kmh in (2.0, 1.0, 0.0):
                await asyncio.sleep(FRAME_GAP)
                pad.c._on_treadmill_data(0, treadmill_frame(kmh))
            await asyncio.sleep(FRAME_GAP)
            pad.c._on_treadmill_data(0, treadmill_frame(1.0))  # belt restarts

        feeder = asyncio.create_task(run_down())
        try:
            return await pad.c.start()
        finally:
            feeder.cancel()

    assert asyncio.run(run()) is True
    start = (FTMSOpcode.START_OR_RESUME, b"")
    assert pad.sent == [start]
    assert pad.speed_at_send == [0.0]


def test_stop_at_rest_does_not_mark_run_down() -> None:
    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.c._machine_status_first_event_seen = True
    pad.c._on_machine_status(0, bytearray([0x02, 0x01]))
    assert pad.c._stopping is False
