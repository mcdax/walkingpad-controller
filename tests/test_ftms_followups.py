"""Follow-ups to the R3 stop handling (#10-#14).

Name-based quirk detection, session-end detection, disconnect wakeups,
cancellation, fresh-telemetry start, and pause() while start() waits.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from walkingpad_controller import FTMSController
from walkingpad_controller.const import FTMSOpcode
from walkingpad_controller.ftms import _needs_pause_before_stop

from test_ftms_stop import FRAME_GAP, PAUSE, STOP, Pad, treadmill_frame

START = (FTMSOpcode.START_OR_RESUME, b"")


# --- #14: quirk detection from the BLE name -------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("KS-AP-RQ3", True),
        ("ks-ap-rq3", True),
        ("KS-HD-Z1D", False),
        ("SPERAX_RM-01_123456", False),
        ("", False),
        (None, False),
    ],
)
def test_pause_before_stop_is_derived_from_the_ble_name(name, expected) -> None:
    assert _needs_pause_before_stop(name) is expected


@pytest.mark.parametrize(
    ("ble_name", "fallback", "expected"),
    [
        ("KS-AP-RQ3", None, True),
        (None, "KS-AP-RQ3", True),  # nameless BLEDevice from a BT proxy
        (None, "KS-HD-Z1D", False),
        (None, None, False),
    ],
)
def test_connect_uses_the_fallback_name_for_the_quirk(
    monkeypatch, ble_name, fallback, expected
) -> None:
    from walkingpad_controller import ftms

    async def establish_connection(*args, **kwargs):
        return SimpleNamespace(is_connected=True)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(ftms, "establish_connection", establish_connection)
    c = FTMSController()
    for method in (
        "_discover_services",
        "_read_capabilities",
        "_subscribe_notifications",
        "_request_control",
    ):
        monkeypatch.setattr(c, method, noop)
    device = SimpleNamespace(name=ble_name, address="AA:BB:CC:DD:EE:FF")
    asyncio.run(c.connect(device, fallback))  # type: ignore[arg-type]
    assert c.capabilities.pause_before_stop is expected


# --- #11: session end as first FM Status event, late session end ---------


def test_session_end_counts_even_as_first_status_event_after_connect() -> None:
    pad = Pad(pause_before_stop=True)
    pad.results[STOP] = [False]
    pad.session_end_on_stop = True
    assert pad.stop(first_status_event_seen=False) is True
    assert pad.sent == [PAUSE, STOP]


def _timed_out_stop_pad(*, session_end_after: float | None) -> Pad:
    """Non-R3 pad whose STOP gets no indication at all."""
    pad = Pad(pause_before_stop=False)
    pad.c._session_end_grace = 0.3
    inner = pad._write

    async def write(opcode, params=b"", *args, **kwargs) -> bool:
        await inner(opcode, params)
        pad.c._last_cp_timed_out = True
        if session_end_after is not None:
            asyncio.get_running_loop().call_later(
                session_end_after,
                pad.c._on_machine_status,
                0,
                bytearray([0x02, 0x01]),
            )
        return False

    pad.c._write_control_point = write  # type: ignore[method-assign]
    return pad


def test_session_end_arriving_after_the_stop_timeout_still_counts() -> None:
    pad = _timed_out_stop_pad(session_end_after=0.05)
    assert pad.stop() is True


def test_timed_out_stop_without_session_end_fails() -> None:
    pad = _timed_out_stop_pad(session_end_after=None)
    assert pad.stop() is False


def test_rejected_stop_does_not_wait_for_a_session_end() -> None:
    pad = Pad(pause_before_stop=False)
    pad.c._session_end_grace = 5.0
    pad.results[STOP] = [False]

    async def run() -> bool:
        return await asyncio.wait_for(pad.c.stop(), 1.0)

    assert asyncio.run(run()) is False


# --- #12: disconnect wakes pending waiters --------------------------------


def _silent_link(c: FTMSController) -> SimpleNamespace:
    """Real Control Point path on a link that never acknowledges."""

    async def write_gatt_char(uuid, data, response=True):
        return None

    async def disconnect():
        link.is_connected = False  # no disconnect callback fired

    link = SimpleNamespace(
        is_connected=True, write_gatt_char=write_gatt_char, disconnect=disconnect
    )
    c._client = link  # type: ignore[assignment]
    c._connected = True
    c._has_control = True
    return link


def test_link_loss_during_control_point_write_fails_fast() -> None:
    c = FTMSController()
    link = _silent_link(c)

    async def run() -> bool:
        def drop() -> None:
            link.is_connected = False
            c._on_disconnect(None)  # type: ignore[arg-type]

        asyncio.get_running_loop().call_later(0.05, drop)
        return await asyncio.wait_for(c.set_target_speed(2.0), 1.0)

    assert asyncio.run(run()) is False


def test_vendor_preamble_write_does_not_assume_success_after_link_loss() -> None:
    c = FTMSController()
    link = _silent_link(c)
    c._capabilities.has_vendor_preamble = True

    async def run() -> bool:
        def drop() -> None:
            link.is_connected = False
            c._on_disconnect(None)  # type: ignore[arg-type]

        asyncio.get_running_loop().call_later(0.05, drop)
        return await asyncio.wait_for(c.pause(), 1.0)

    assert asyncio.run(run()) is False
    assert c._status_ack_expected_opcode is None


def test_explicit_disconnect_wakes_a_stop_waiting_for_standstill() -> None:
    pad = Pad(pause_before_stop=True)
    pad.on_pause = []  # no telemetry
    pad.c._standstill_timeout = 5.0
    pad.c._client.disconnect = _noop_disconnect(pad)  # type: ignore[union-attr]

    async def run() -> bool:
        stop = asyncio.create_task(pad.c.stop())
        await asyncio.sleep(0.05)
        await pad.c.disconnect()
        return await asyncio.wait_for(stop, 1.0)

    assert asyncio.run(run()) is False
    assert pad.sent == [PAUSE]


def _noop_disconnect(pad: Pad):
    async def disconnect() -> None:
        pad.c._client.is_connected = False  # type: ignore[union-attr]

    return disconnect


# --- #14: cancellation releases locks and the ack expectation -------------


def test_cancelling_stop_releases_both_locks() -> None:
    pad = Pad(pause_before_stop=True)
    pad.on_pause = []
    pad.c._standstill_timeout = 5.0

    async def run() -> None:
        stop = asyncio.create_task(pad.c.stop())
        await asyncio.sleep(0.05)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert not pad.c._op_lock.locked()
        assert not pad.c._cp_lock.locked()

    asyncio.run(run())


def test_cancelling_a_control_point_write_clears_the_ack_expectation() -> None:
    c = FTMSController()
    _silent_link(c)
    c._capabilities.has_vendor_preamble = True

    async def run() -> None:
        write = asyncio.create_task(c.pause())
        await asyncio.sleep(0.05)
        assert c._status_ack_expected_opcode is not None
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert c._status_ack_expected_opcode is None
        assert not c._cp_lock.locked()

    asyncio.run(run())


# --- #14: pause() is not blocked by start() waiting for the belt ----------


def test_pause_goes_out_while_start_waits_for_the_belt() -> None:
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.on_pause = []

    async def run() -> None:
        start = asyncio.create_task(pad.c.start())
        await asyncio.sleep(0.05)
        assert not start.done()  # waiting for the belt to move
        assert await asyncio.wait_for(pad.c.pause(), 0.5) is True
        # start() gives up right away instead of holding the op lock for
        # its full timeout or reporting success
        assert await asyncio.wait_for(start, 0.5) is False
        assert not pad.c._op_lock.locked()

    asyncio.run(run())
    assert pad.sent == [START, PAUSE]


def test_set_speed_does_not_reaccelerate_after_pause_during_start() -> None:
    from walkingpad_controller import WalkingPadController

    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.on_pause = []
    wp = WalkingPadController.__new__(WalkingPadController)
    wp._ftms = pad.c
    wp._wilink = None
    wp._sperax = None

    async def run() -> bool:
        set_speed = asyncio.create_task(wp.set_speed(4.0))
        await asyncio.sleep(0.05)
        await pad.c.pause()
        pad.c._on_treadmill_data(0, treadmill_frame(0.8))  # spin-up frame
        return await asyncio.wait_for(set_speed, 0.5)

    assert asyncio.run(run()) is False
    assert [op for op, _ in pad.sent] == [
        FTMSOpcode.START_OR_RESUME,
        FTMSOpcode.STOP_OR_PAUSE,
    ]


def test_rejected_start_while_decelerating_after_pause_is_not_success() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)
    pad.results[START] = [False]
    pad.c._paused = True

    async def run() -> bool:
        asyncio.get_running_loop().call_later(
            FRAME_GAP, pad.c._on_treadmill_data, 0, treadmill_frame(2.0)
        )
        return await pad.c.start()

    assert asyncio.run(run()) is False


# --- #13: start() needs fresh telemetry, not the cached speed --------------


def test_start_ignores_a_stale_cached_speed() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)  # left over, no fresh frame

    async def run() -> None:
        start = asyncio.create_task(pad.c.start())
        await asyncio.sleep(0.1)
        assert not start.done()
        pad.c._on_treadmill_data(0, treadmill_frame(1.0))
        assert await asyncio.wait_for(start, 0.5) is True

    asyncio.run(run())


def test_rejected_start_with_stale_cached_speed_is_not_success() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)
    pad.results[START] = [False]

    async def run() -> bool:
        return await pad.c.start()

    assert asyncio.run(run()) is False


def test_rejected_start_while_belt_is_running_is_success() -> None:
    pad = Pad(pause_before_stop=False, speed=3.0)
    pad.results[START] = [False]

    async def run() -> bool:
        asyncio.get_running_loop().call_later(
            FRAME_GAP, pad.c._on_treadmill_data, 0, treadmill_frame(3.0)
        )
        return await pad.c.start()

    assert asyncio.run(run()) is True


# --- #10: no countdown watch when the session is known to have ended ------


def test_stop_at_rest_skips_the_countdown_watch_after_a_session_end() -> None:
    pad = Pad(pause_before_stop=True, speed=0.0)
    pad.on_pause = [0.0]
    pad.results[PAUSE] = [False]
    pad.results[STOP] = [False]
    pad.c._countdown_grace = 5.0
    pad.c._session_active = False

    async def run() -> bool:
        pad.c._machine_status_first_event_skipped = True
        return await asyncio.wait_for(pad.c.stop(), 1.0)

    assert asyncio.run(run()) is False
    assert pad.sent == [PAUSE, STOP]


def test_session_state_follows_status_events_and_telemetry() -> None:
    c = FTMSController()
    c._machine_status_first_event_skipped = True
    assert c._session_active is None
    c._on_machine_status(0, bytearray([0x02, 0x01]))
    assert c._session_active is False
    c._on_machine_status(0, bytearray([0x04]))
    assert c._session_active is True
    c._on_machine_status(0, bytearray([0x02, 0x01]))
    c._on_treadmill_data(0, treadmill_frame(0.5))
    assert c._session_active is True
    c._on_disconnect(None)  # type: ignore[arg-type]
    assert c._session_active is None


def test_motion_wait_sees_a_moving_frame_followed_by_a_still_one() -> None:
    """Two frames delivered before the waiter resumes: 1 km/h, then 0."""
    from walkingpad_controller.ftms import _Standstill

    pad = Pad(pause_before_stop=False, speed=0.0)

    async def run() -> _Standstill:
        def two_frames() -> None:
            pad.c._on_treadmill_data(0, treadmill_frame(1.0))
            pad.c._on_treadmill_data(0, treadmill_frame(0.0))

        seq = pad.c._treadmill_data_seq
        asyncio.get_running_loop().call_later(0.02, two_frames)
        return await pad.c._wait_for_motion(seq, 0.3)

    assert asyncio.run(run()) is _Standstill.STILL_MOVING


def test_start_needs_the_latest_frame_to_show_motion() -> None:
    """A coasting frame then 0 km/h during START is not a running belt."""
    pad = Pad(pause_before_stop=False, speed=0.0)

    async def run() -> bool:
        def two_frames() -> None:
            pad.c._on_treadmill_data(0, treadmill_frame(0.5))
            pad.c._on_treadmill_data(0, treadmill_frame(0.0))

        asyncio.get_running_loop().call_later(0.02, two_frames)
        start = asyncio.create_task(pad.c.start())
        await asyncio.sleep(0.1)
        assert not start.done()
        pad.c._on_treadmill_data(0, treadmill_frame(1.0))
        return await asyncio.wait_for(start, 0.5)

    assert asyncio.run(run()) is True


def test_pause_during_request_control_abandons_start() -> None:
    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.c._has_control = False
    inner = pad._write

    async def write(opcode, params=b"", *args, **kwargs) -> bool:
        if opcode == FTMSOpcode.REQUEST_CONTROL:
            pad.c._pause_seq += 1  # pause() lands while REQUEST_CONTROL is in flight
        return await inner(opcode, params)

    pad.c._write_control_point = write  # type: ignore[method-assign]
    assert asyncio.run(pad.c.start()) is False
    assert FTMSOpcode.START_OR_RESUME not in [op for op, _ in pad.sent]


def test_successful_stop_marks_the_session_ended() -> None:
    pad = Pad(pause_before_stop=True)
    pad.results[STOP] = [False]
    pad.session_end_on_stop = True
    assert pad.stop(first_status_event_seen=False) is True
    assert pad.c._session_active is False
