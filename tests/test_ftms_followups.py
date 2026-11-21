"""Follow-ups to the R3 stop handling (#10-#14).

Name-based quirk detection, session-end detection, disconnect wakeups,
cancellation, fresh-telemetry start, and pause() while start() waits.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from walkingpad_controller import FTMSController
from walkingpad_controller.const import BeltState, FTMSOpcode
from walkingpad_controller.ftms import _CPResult, _needs_pause_before_stop

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
        "_ensure_telemetry",
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

    async def write(opcode, params=b"", *args, **kwargs) -> _CPResult:
        await inner(opcode, params)
        if session_end_after is not None:
            asyncio.get_running_loop().call_later(
                session_end_after,
                pad.c._on_machine_status,
                0,
                bytearray([0x02, 0x01]),
            )
        return _CPResult.TIMEOUT

    pad.c._write_control_point_result = write  # type: ignore[method-assign]
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

    async def write(opcode, params=b"", *args, **kwargs) -> _CPResult:
        if opcode == FTMSOpcode.REQUEST_CONTROL:
            pad.c._pause_seq += 1  # pause() lands while REQUEST_CONTROL is in flight
        return await pad._write_result(opcode, params)

    pad.c._write_control_point_result = write  # type: ignore[method-assign]
    assert asyncio.run(pad.c.start()) is False
    assert FTMSOpcode.START_OR_RESUME not in [op for op, _ in pad.sent]




# --- #16: first-event skip, notification order, state after disconnect ---


def test_status_replay_right_after_subscribe_is_dropped() -> None:
    import time

    c = FTMSController()
    c._status_subscribed_at = time.monotonic()
    c._on_machine_status(0, bytearray([0x02, 0x02]))
    assert c._paused is False
    assert c.status.last_fm_event is None or c.status.last_fm_event != 0x02


def test_first_real_status_event_later_after_subscribe_is_kept() -> None:
    import time

    c = FTMSController()
    c._status_subscribed_at = time.monotonic() - 5.0
    c._on_machine_status(0, bytearray([0x02, 0x02]))  # first PAUSE after connect
    assert c._paused is True


def test_status_callbacks_see_the_updated_state() -> None:
    c = FTMSController()
    c._machine_status_first_event_skipped = True
    seen = []
    c.register_status_callback(lambda st: seen.append((c._paused, st.belt_state)))
    c._on_machine_status(0, bytearray([0x02, 0x02]))
    assert seen[-1] == (True, BeltState.PAUSED)


def test_session_end_at_rest_reports_stopped_without_another_frame() -> None:
    c = FTMSController()
    c._machine_status_first_event_skipped = True
    c._on_machine_status(0, bytearray([0x02, 0x02]))
    c._on_treadmill_data(0, treadmill_frame(0.0))
    assert c.status.belt_state == BeltState.PAUSED
    c._on_machine_status(0, bytearray([0x02, 0x01]))
    assert c.status.belt_state == BeltState.STOPPED


def test_disconnect_forgets_speed_and_pause_state() -> None:
    pad = Pad(pause_before_stop=False, speed=0.0)
    pad.c._client.disconnect = _noop_disconnect(pad)  # type: ignore[union-attr]
    pad.c._on_treadmill_data(0, treadmill_frame(3.0))
    pad.c._paused = True
    pad.c._machine_status_first_event_skipped = True
    asyncio.run(pad.c.disconnect())
    assert pad.c.status.speed == 0
    assert pad.c.status.belt_state == BeltState.STOPPED
    assert pad.c._paused is False
    assert pad.c._machine_status_first_event_skipped is False


def _answering_link(c: FTMSController, answer) -> list[bytes]:
    """Real Control Point path; ``answer(data)`` returns indications to send."""
    writes: list[bytes] = []

    async def write_gatt_char(uuid, data, response=True):
        writes.append(bytes(data))
        loop = asyncio.get_running_loop()
        for delay, frame in answer(bytes(data)):
            loop.call_later(delay, c._on_control_point_response, 0, bytearray(frame))

    c._client = SimpleNamespace(  # type: ignore[assignment]
        is_connected=True, write_gatt_char=write_gatt_char
    )
    c._connected = True
    c._has_control = True
    return writes


def test_leftover_wake_up_does_not_turn_a_rejected_stop_into_success() -> None:
    c = FTMSController()
    _answering_link(c, lambda data: [(0.01, [0x80, data[0], 0x04])])
    c._wake_waiters()  # e.g. left over from a previous connection
    assert asyncio.run(c.stop()) is False


def test_late_indication_for_another_opcode_is_ignored() -> None:
    c = FTMSController()
    # SET_TARGET_SPEED: first a late REQUEST_CONTROL indication, then its own
    _answering_link(
        c, lambda data: [(0.01, [0x80, 0x00, 0x01]), (0.03, [0x80, data[0], 0x01])]
    )
    assert asyncio.run(c.set_target_speed(2.0)) is True


# --- #17: vendor pre-amble acks -------------------------------------------


def test_preamble_stop_is_not_acknowledged_by_a_pause_event() -> None:
    c = FTMSController()
    c._capabilities.has_vendor_preamble = True
    c._machine_status_first_event_skipped = True

    def answer(data: bytes):
        if data[:1] == bytes([FTMSOpcode.STOP_OR_PAUSE]):
            asyncio.get_running_loop().call_later(
                0.01, c._on_machine_status, 0, bytearray([0x02, 0x02])
            )
        return []

    _answering_link(c, answer)
    c._session_end_grace = 0.05

    async def run() -> _CPResult:
        return await c._write_control_point_result(
            FTMSOpcode.STOP_OR_PAUSE, bytes([0x01]), timeout=0.2
        )

    assert asyncio.run(run()) is not _CPResult.ACK


def test_preamble_pause_is_acknowledged_by_a_pause_event() -> None:
    c = FTMSController()
    c._capabilities.has_vendor_preamble = True
    c._machine_status_first_event_skipped = True

    def answer(data: bytes):
        if data[:1] == bytes([FTMSOpcode.STOP_OR_PAUSE]):
            asyncio.get_running_loop().call_later(
                0.01, c._on_machine_status, 0, bytearray([0x02, 0x02])
            )
        return []

    _answering_link(c, answer)
    assert asyncio.run(c.pause()) is True


# --- #18: Treadmill Data parsing and speed rounding -----------------------


def test_truncated_treadmill_frame_does_not_misparse_later_fields() -> None:
    import struct

    from walkingpad_controller.const import TreadmillDataFlags

    c = FTMSController()
    flags = TreadmillDataFlags.TOTAL_DISTANCE | TreadmillDataFlags.HEART_RATE
    c._on_treadmill_data(0, bytearray(struct.pack("<HH", flags, 200) + b"\x99\x00"))
    assert c.status.speed == 2.0
    assert c.status.heart_rate == 0
    assert c.status.distance == 0


def test_full_treadmill_frame_still_parses() -> None:
    import struct

    from walkingpad_controller.const import TreadmillDataFlags

    c = FTMSController()
    flags = (
        TreadmillDataFlags.TOTAL_DISTANCE
        | TreadmillDataFlags.EXPENDED_ENERGY
        | TreadmillDataFlags.ELAPSED_TIME
        | TreadmillDataFlags.KINGSMITH_EXTENSION
    )
    frame = (
        struct.pack("<HH", flags, 350)
        + bytes([0x10, 0x27, 0x00])  # 10000 m
        + struct.pack("<HHB", 120, 300, 0)
        + struct.pack("<H", 1800)
        + struct.pack("<HB", 4321, 0)
    )
    c._on_treadmill_data(0, bytearray(frame))
    assert (c.status.speed, c.status.distance, c.status.calories) == (3.5, 10000, 120)
    assert (c.status.duration, c.status.steps) == (1800, 4321)


def test_speed_rounding_stays_inside_the_supported_range() -> None:
    from walkingpad_controller.models import SpeedRange

    pad = Pad(pause_before_stop=False)
    pad.c._capabilities.speed_range = SpeedRange(min_speed=0.8, max_speed=6.0, increment=0.3)
    asyncio.run(pad.c.set_target_speed(0.8))
    (_, params), = pad.sent
    assert struct_speed(params) >= 0.8


def struct_speed(params: bytes) -> float:
    import struct

    return struct.unpack("<H", params)[0] / 100


# --- subscriptions --------------------------------------------------------


def _subscribing_client(fail_uuid: str | None):
    subscribed: list[str] = []

    async def start_notify(uuid, handler):
        if uuid == fail_uuid:
            raise RuntimeError("GATT error")
        subscribed.append(uuid)

    return SimpleNamespace(is_connected=True, start_notify=start_notify), subscribed


@pytest.mark.parametrize("required", [True, False])
def test_essential_subscription_failure_fails_the_connect(monkeypatch, required) -> None:
    from bleak.exc import BleakError

    from walkingpad_controller import ftms
    from walkingpad_controller.const import (
        FITNESS_MACHINE_STATUS_UUID,
        TREADMILL_DATA_UUID,
    )

    monkeypatch.setattr(ftms.asyncio, "sleep", _no_sleep)
    c = FTMSController()
    fail = TREADMILL_DATA_UUID if required else FITNESS_MACHINE_STATUS_UUID
    c._client, subscribed = _subscribing_client(fail)  # type: ignore[assignment]
    if required:
        with pytest.raises(BleakError):
            asyncio.run(c._subscribe_notifications())
    else:
        asyncio.run(c._subscribe_notifications())
        assert len(subscribed) == 2


async def _no_sleep(*args, **kwargs):
    return None


def test_subscribe_training_status_fills_the_status() -> None:
    c = FTMSController()
    handlers = {}

    async def start_notify(uuid, handler):
        handlers[uuid] = handler

    c._client = SimpleNamespace(is_connected=True, start_notify=start_notify)  # type: ignore[assignment]
    c._connected = True
    assert asyncio.run(c.subscribe_training_status()) is True
    (handler,) = handlers.values()
    handler(0, bytearray([0x00, 0x0D]))
    assert c.status.training_status == 0x0D


# --- R3: CCCD writes ignored right after connect (docs §2.6.1) ------------


def _r3_link(c: FTMSController, *, deliver_after_subscribes: int | None):
    """Link whose notifications only flow after N start_notify rounds."""
    from walkingpad_controller.const import TREADMILL_DATA_UUID

    calls = {"subscribe": 0, "stop": 0}

    async def start_notify(uuid, handler):
        if uuid != TREADMILL_DATA_UUID:
            return
        calls["subscribe"] += 1
        if deliver_after_subscribes is not None and calls["subscribe"] >= deliver_after_subscribes:
            asyncio.get_running_loop().call_later(
                0.02, handler, 0, treadmill_frame(0.0)
            )

    async def stop_notify(uuid):
        calls["stop"] += 1

    c._client = SimpleNamespace(  # type: ignore[assignment]
        is_connected=True, start_notify=start_notify, stop_notify=stop_notify
    )
    c._connected = True
    c._telemetry_check_timeout = 0.1
    return calls


def test_r3_resubscribes_when_no_telemetry_arrives(monkeypatch) -> None:
    from walkingpad_controller import ftms

    monkeypatch.setattr(ftms.asyncio, "sleep", _no_sleep)
    c = FTMSController()
    calls = _r3_link(c, deliver_after_subscribes=2)

    async def run() -> None:
        await c._subscribe_notifications()
        await c._ensure_telemetry()

    asyncio.run(run())
    assert calls["subscribe"] == 2 and calls["stop"] == 3


def test_r3_connect_continues_when_telemetry_never_arrives(monkeypatch) -> None:
    from walkingpad_controller import ftms

    monkeypatch.setattr(ftms.asyncio, "sleep", _no_sleep)
    c = FTMSController()
    calls = _r3_link(c, deliver_after_subscribes=None)

    async def run() -> None:
        await c._subscribe_notifications()
        c._machine_status_first_event_skipped = True
        await c._ensure_telemetry()

    asyncio.run(run())  # silent idle state is not a failed connect
    assert calls["subscribe"] == 2
    assert c._machine_status_first_event_skipped is False  # replay re-armed


def test_r3_with_flowing_telemetry_does_not_resubscribe(monkeypatch) -> None:
    from walkingpad_controller import ftms

    monkeypatch.setattr(ftms.asyncio, "sleep", _no_sleep)
    c = FTMSController()
    calls = _r3_link(c, deliver_after_subscribes=1)

    async def run() -> None:
        await c._subscribe_notifications()
        await c._ensure_telemetry()

    asyncio.run(run())
    assert calls == {"subscribe": 1, "stop": 0}
