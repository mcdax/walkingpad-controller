"""Stress test + integrated BLE trace, in one process.

Single connection (the device only allows one client). The orchestrator
runs commands as before; in parallel we hook every notifiable char on the
same client and log every received frame with a timestamp aligned to the
command timeline. Output is interleaved chronologically so a failed
set_speed shows whether the device fired TARGET_SPEED_CHANGED behind
the scenes (indication channel issue) or fired nothing (command really
not accepted).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import struct
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from commands._common import (  # noqa: E402
    DEFAULT_DEVICE_NAME,
    connect,
    safe_disconnect,
    setup_logging,
    snapshot,
)
from commands import (  # noqa: E402
    test_set_speed, test_start, test_stop, test_switch_mode, test_update_state,
    test_pause, test_reset, test_set_inclination,
)


_LOGGER = logging.getLogger("traced")

CHAR_NAME = {
    "00002acd-0000-1000-8000-00805f9b34fb": "Treadmill Data",
    "00002ada-0000-1000-8000-00805f9b34fb": "FM Status",
    "00002ad9-0000-1000-8000-00805f9b34fb": "CP Indication",
    "00002ad3-0000-1000-8000-00805f9b34fb": "Training Status",
}


def decode_treadmill(d: bytes) -> str:
    if len(d) < 4:
        return "(short)"
    flags = struct.unpack_from("<H", d, 0)[0]
    speed = struct.unpack_from("<H", d, 2)[0] / 100.0
    parts = [f"speed={speed:.2f}"]
    off = 4
    if flags & 0x0004 and off + 3 <= len(d):
        parts.append(f"dist={d[off] | (d[off+1]<<8) | (d[off+2]<<16)}m"); off += 3
    if flags & 0x0080 and off + 5 <= len(d):
        parts.append(f"cal={struct.unpack_from('<H', d, off)[0]}"); off += 5
    if flags & 0x0400 and off + 2 <= len(d):
        parts.append(f"t={struct.unpack_from('<H', d, off)[0]}s"); off += 2
    if flags & 0x2000 and off + 3 <= len(d):
        parts.append(f"steps={struct.unpack_from('<H', d, off)[0]}"); off += 3
    return " ".join(parts)


def decode_fm_status(d: bytes) -> str:
    if not d:
        return "(empty)"
    op = d[0]
    NAMES = {1: "RESET", 2: "STOPPED", 3: "SAFETY_STOP", 4: "STARTED",
             5: "TARGET_SPEED", 6: "TARGET_INCLINE", 0x14: "SPIN_DOWN",
             0xFF: "CONTROL_LOST"}
    name = NAMES.get(op, f"0x{op:02x}")
    if op == 0x05 and len(d) >= 3:
        v = struct.unpack_from("<H", d, 1)[0] / 100.0
        return f"{name}={v:.2f}km/h"
    if op == 0x02 and len(d) >= 2:
        return f"{name} param=0x{d[1]:02x}"
    return name


def decode_cp(d: bytes) -> str:
    if len(d) < 3 or d[0] != 0x80:
        return d.hex()
    return f"resp opc=0x{d[1]:02x} result=0x{d[2]:02x}"


def decode_training(d: bytes) -> str:
    if len(d) < 2:
        return d.hex()
    NAMES = {0: "OTHER", 1: "IDLE", 2: "WARMING", 12: "MANUAL",
             13: "PRE_WORKOUT", 14: "POST_WORKOUT"}
    return f"flags=0x{d[0]:02x} status={NAMES.get(d[1], hex(d[1]))}"


DECODERS = {
    "00002acd-0000-1000-8000-00805f9b34fb": decode_treadmill,
    "00002ada-0000-1000-8000-00805f9b34fb": decode_fm_status,
    "00002ad9-0000-1000-8000-00805f9b34fb": decode_cp,
    "00002ad3-0000-1000-8000-00805f9b34fb": decode_training,
}


COMMANDS = {
    "start":           (test_start.fn,            lambda: argparse.Namespace(name="", log="W", json=False, settle=2.0)),
    "stop":            (test_stop.fn,             lambda: argparse.Namespace(name="", log="W", json=False, start_first=False, run_for=0.0)),
    "set_speed":       (test_set_speed.fn,        lambda v: argparse.Namespace(name="", log="W", json=False, speed=v, sweep=None, start_first=False, settle=0, gap=1.0)),
    "switch_mode":     (test_switch_mode.fn,      lambda m: argparse.Namespace(name="", log="W", json=False, mode=m)),
    "update_state":    (test_update_state.fn,     lambda: argparse.Namespace(name="", log="W", json=False)),
    "pause":           (test_pause.fn,            lambda: argparse.Namespace(name="", log="W", json=False, start_first=False, run_for=0.0)),
    "reset":           (test_reset.fn,            lambda: argparse.Namespace(name="", log="W", json=False)),
    "set_inclination": (test_set_inclination.fn,  lambda p: argparse.Namespace(name="", log="W", json=False, percent=p)),
}


# Predefined sequences focused on reproducing the set_speed-timeout pattern.
SEQUENCES: dict[str, list[tuple]] = {
    "happy": [
        ("start", ()), ("set_speed", (2.5,)), ("set_speed", (3.0,)), ("stop", ()),
    ],
    "minspeed_setspeed": [
        # Repro target: cold-start leaves belt at 1.0 (min), then set_speed at min
        ("start", ()), ("set_speed", (1.5,)), ("set_speed", (2.0,)),
        ("set_speed", (2.5,)), ("stop", ()),
    ],
    "after_reset": [
        ("reset", ()), ("start", ()), ("set_speed", (2.0,)), ("stop", ()),
    ],
    "after_standby": [
        ("switch_mode", ("standby",)), ("start", ()), ("set_speed", (2.0,)), ("stop", ()),
    ],
    "stop_start_cycles": [
        ("start", ()), ("set_speed", (2.5,)), ("stop", ()),
        ("start", ()), ("set_speed", (2.5,)), ("stop", ()),
    ],
}


class TraceLog:
    def __init__(self):
        self.events: deque = deque()
        self.t0 = time.time()

    def cmd(self, label: str):
        self.events.append((time.time() - self.t0, "CMD", label))
        return time.time() - self.t0

    def cmd_done(self, label: str, ok: bool, lat: float, extra: str = ""):
        self.events.append((time.time() - self.t0, "DONE", f"{label} ok={ok} lat={lat:.0f}ms {extra}"))

    def notif(self, char_uuid: str, data: bytes):
        decoder = DECODERS.get(char_uuid)
        decoded = decoder(data) if decoder else data.hex()
        name = CHAR_NAME.get(char_uuid, char_uuid[:8])
        self.events.append((time.time() - self.t0, "RX", f"{name}: {decoded}"))

    def dump(self, fp):
        for ts, kind, msg in self.events:
            fp.write(f"  [{ts:8.3f}]  {kind:4s}  {msg}\n")


async def run_sequence(name: str, seq: list[tuple], inter_cmd_gap: float, trace: TraceLog):
    controller = await connect(name=name)
    if controller is None:
        _LOGGER.error("connect failed")
        return None

    # Hook training-status notifications too (lib doesn't subscribe to it by
    # default but it's the smoking gun for the PRE_WORKOUT theory).
    extras_subscribed = []
    client = controller._ftms._client  # type: ignore[union-attr]
    for uuid in ("00002ad3-0000-1000-8000-00805f9b34fb",):
        try:
            await client.start_notify(
                uuid, lambda _h, d, u=uuid: trace.notif(u, bytes(d))
            )
            extras_subscribed.append(uuid)
        except Exception as err:
            _LOGGER.warning("subscribe %s failed: %s", uuid, err)

    # Re-route the lib's existing notification handlers so they also feed
    # the trace.
    orig_treadmill_handler = controller._ftms._on_treadmill_data  # type: ignore[union-attr]
    orig_status_handler = controller._ftms._on_machine_status  # type: ignore[union-attr]
    orig_cp_handler = controller._ftms._on_control_point_response  # type: ignore[union-attr]

    TM_UUID = "00002acd-0000-1000-8000-00805f9b34fb"
    FS_UUID = "00002ada-0000-1000-8000-00805f9b34fb"
    CP_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"

    def tap(uuid, orig):
        def inner(sender, data):
            trace.notif(uuid, bytes(data))
            return orig(sender, data)
        return inner

    # Replace on the underlying client
    with contextlib.suppress(Exception):
        await client.stop_notify(TM_UUID)
        await client.start_notify(TM_UUID, tap(TM_UUID, orig_treadmill_handler))
    with contextlib.suppress(Exception):
        await client.stop_notify(FS_UUID)
        await client.start_notify(FS_UUID, tap(FS_UUID, orig_status_handler))
    with contextlib.suppress(Exception):
        await client.stop_notify(CP_UUID)
        await client.start_notify(CP_UUID, tap(CP_UUID, orig_cp_handler))

    try:
        for cmd_name, args_tuple in seq:
            fn, factory = COMMANDS[cmd_name]
            label = f"{cmd_name}{args_tuple if args_tuple else ''}"
            args = factory(*args_tuple) if args_tuple else factory()
            t0 = trace.cmd(label)
            t_real = time.time()
            try:
                extra = await fn(controller, args)
                ok = bool(extra.get("ok", True))
                trace.cmd_done(label, ok, (time.time() - t_real) * 1000.0, str(extra))
            except Exception as err:  # noqa: BLE001
                trace.cmd_done(label, False, (time.time() - t_real) * 1000.0, f"raised: {err}")
            await asyncio.sleep(inter_cmd_gap)
    finally:
        with contextlib.suppress(Exception):
            if controller.connected and controller.status.speed > 0:
                await controller.stop()
        await safe_disconnect(controller)
    return trace


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--sequence", choices=list(SEQUENCES), default="minspeed_setspeed")
    p.add_argument("--inter-cmd-gap", type=float, default=2.0)
    p.add_argument("--out", type=Path, default=None,
                   help="write trace here (default: stdout)")
    p.add_argument("--log", default="WARNING")
    args = p.parse_args()
    setup_logging(args.log)

    trace = TraceLog()
    print(f"Running sequence {args.sequence!r} with notification trace…")
    print(f"  inter-cmd gap: {args.inter_cmd_gap}s")
    print(f"  steps: {' -> '.join(c for c, _ in SEQUENCES[args.sequence])}")
    print()

    await run_sequence(args.name, SEQUENCES[args.sequence], args.inter_cmd_gap, trace)

    fp = open(args.out, "w") if args.out else sys.stdout
    fp.write("\nTRACE (chronological):\n\n")
    trace.dump(fp)
    if args.out:
        fp.close()
        print(f"\ntrace written to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
