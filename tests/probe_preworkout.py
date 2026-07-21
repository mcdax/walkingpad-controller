"""Rigorous probe of the 'PRE_WORKOUT blocks SET_TARGET_SPEED' hypothesis.

Runs four experiments, each with the BLE notification trace recorded
inline. Every set_speed records the device's training_status at the
moment of the write and the outcome. We then tabulate the correlation
to confirm or falsify the hypothesis.

Experiments:
  E1: after_reset × 3
      Reproducibility — does reset+start+set_speed always fail?
  E2: cold_start_then_setspeed_repeated
      After a cold start, try set_speed every 5s for 30s. Does any
      set_speed succeed once enough time has passed in PRE_WORKOUT?
  E3: cold_start_then_set_inclination
      Is the block specific to SET_TARGET_SPEED, or does it apply to
      any target-setting opcode (e.g. SET_TARGET_INCLINATION)?
  E4: stop_start_setspeed × 3
      "Resume" path (start while belt is decelerating). Does set_speed
      work in this state?

Output: per-attempt rows + a falsification check. Hypothesis is
falsified if any set_speed in PRE_WORKOUT succeeded, or any set_speed
out of PRE_WORKOUT failed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from commands._common import DEFAULT_DEVICE_NAME, connect, safe_disconnect, setup_logging  # noqa: E402

_LOGGER = logging.getLogger("preworkout")

TRAINING_STATUS_UUID = "00002ad3-0000-1000-8000-00805f9b34fb"
FM_STATUS_UUID = "00002ada-0000-1000-8000-00805f9b34fb"
TM_DATA_UUID = "00002acd-0000-1000-8000-00805f9b34fb"


@dataclass
class Attempt:
    experiment: str
    rep: int
    target_speed: float | None
    target_incline: float | None
    training_status_at_write: int | None
    last_fm_event: int | None
    speed_before: float
    speed_after: float
    cmd_ok: bool  # what the lib reported
    actually_changed: bool  # did the speed move toward target by >= 0.5
    elapsed_since_start_s: float
    extra: str = ""


class TraceState:
    def __init__(self):
        self.training_status: int | None = None
        self.last_fm_event: int | None = None
        self.last_speed: float = 0.0
        self.events: deque = deque()
        self.t0 = time.time()

    def record(self, kind: str, msg: str):
        self.events.append((time.time() - self.t0, kind, msg))

    def on_training_status(self, _h, data: bytes):
        if len(data) >= 2:
            self.training_status = data[1]
            self.record("ts", f"Training Status flags=0x{data[0]:02x} status={data[1]}")

    def on_fm_status(self, _h, data: bytes):
        if data:
            self.last_fm_event = data[0]
            self.record("fm", f"FM Status op=0x{data[0]:02x} data={bytes(data).hex()}")

    def on_treadmill_data(self, _h, data: bytes):
        if len(data) >= 4:
            self.last_speed = struct.unpack_from("<H", bytes(data), 2)[0] / 100.0


async def setup_trace(controller, ts: TraceState):
    """The library already subscribes to Training Status / Treadmill Data /
    FM Status and tracks them in controller.status. We mirror by reading
    that struct rather than fighting Bleak for a second subscription."""

    async def _poll():
        try:
            while True:
                s = controller.status
                if s.training_status != ts.training_status:
                    ts.training_status = s.training_status
                    ts.record("ts", f"training_status -> {s.training_status}")
                if s.last_fm_event != ts.last_fm_event:
                    ts.last_fm_event = s.last_fm_event
                    ts.record("fm", f"last_fm_event -> 0x{s.last_fm_event:02x}")
                ts.last_speed = s.speed
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return

    ts._poll_task = asyncio.create_task(_poll())  # type: ignore[attr-defined]


async def teardown_trace(ts: TraceState):
    task = getattr(ts, "_poll_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def attempt_set_speed(controller, ts: TraceState, target: float,
                            experiment: str, rep: int, t_start: float) -> Attempt:
    s_before = controller.status
    speed_before = s_before.speed
    training_at_write = s_before.training_status
    fm_at_write = s_before.last_fm_event
    ok = await controller.set_speed(target)
    await asyncio.sleep(2.0)
    speed_after = controller.status.speed
    return Attempt(
        experiment=experiment, rep=rep,
        target_speed=target, target_incline=None,
        training_status_at_write=training_at_write,
        last_fm_event=fm_at_write,
        speed_before=speed_before, speed_after=speed_after,
        cmd_ok=ok,
        actually_changed=abs(speed_after - target) < 0.3,
        elapsed_since_start_s=time.time() - t_start,
    )


async def attempt_set_inclination(controller, ts: TraceState, percent: float,
                                  experiment: str, rep: int, t_start: float) -> Attempt:
    s_before = controller.status
    training_at_write = s_before.training_status
    fm_at_write = s_before.last_fm_event
    speed_before = s_before.speed
    ok = False
    err = ""
    try:
        ok = await controller._ftms.set_target_inclination(percent)
    except Exception as e:
        err = str(e)
    await asyncio.sleep(2.0)
    return Attempt(
        experiment=experiment, rep=rep,
        target_speed=None, target_incline=percent,
        training_status_at_write=training_at_write,
        last_fm_event=fm_at_write,
        speed_before=speed_before, speed_after=controller.status.speed,
        cmd_ok=ok,
        actually_changed=False,
        elapsed_since_start_s=time.time() - t_start,
        extra=err,
    )


async def safe_full_stop(controller):
    with contextlib.suppress(Exception):
        if controller.connected:
            await controller.stop()
    await asyncio.sleep(2.0)


async def experiment_after_reset(controller, ts: TraceState, rep: int) -> list[Attempt]:
    _LOGGER.info("E1 rep %d: reset + start + set_speed", rep)
    await controller._ftms.reset()
    await asyncio.sleep(2.0)
    t_cold = time.time()
    await controller.start()
    await asyncio.sleep(3.0)  # let belt settle at min speed
    a = await attempt_set_speed(controller, ts, 2.0, "E1_after_reset", rep, t_cold)
    await safe_full_stop(controller)
    return [a]


async def experiment_cold_setspeed_repeated(controller, ts: TraceState, rep: int) -> list[Attempt]:
    """After a cold start, try set_speed every 5s for 30s — does PRE_WORKOUT
    eventually time out on its own and let the speed change through?"""
    _LOGGER.info("E2 rep %d: cold start + set_speed every 5s for 30s", rep)
    await controller._ftms.reset()
    await asyncio.sleep(2.0)
    t_cold = time.time()
    await controller.start()
    await asyncio.sleep(3.0)
    attempts = []
    targets = [2.0, 2.5, 3.0, 1.5, 2.0, 2.5]
    for i, tg in enumerate(targets):
        a = await attempt_set_speed(controller, ts, tg, "E2_cold_repeated", rep, t_cold)
        attempts.append(a)
        await asyncio.sleep(3.0)  # 5s total between attempts
    await safe_full_stop(controller)
    return attempts


async def experiment_set_inclination(controller, ts: TraceState, rep: int) -> list[Attempt]:
    _LOGGER.info("E3 rep %d: cold start + set_inclination", rep)
    await controller._ftms.reset()
    await asyncio.sleep(2.0)
    t_cold = time.time()
    await controller.start()
    await asyncio.sleep(3.0)
    a = await attempt_set_inclination(controller, ts, 0.0, "E3_set_incline", rep, t_cold)
    await safe_full_stop(controller)
    return [a]


async def experiment_stop_start_setspeed(controller, ts: TraceState, rep: int) -> list[Attempt]:
    """Stop a running belt, then start (=resume), then set_speed.
    Does set_speed work in the 'resumed without going through a fresh
    cold start' state?"""
    _LOGGER.info("E4 rep %d: stop -> start (resume) -> set_speed", rep)
    if controller.status.speed == 0:
        await controller.start()
        await asyncio.sleep(4.0)
    await controller.stop()
    await asyncio.sleep(2.0)  # short gap — belt likely still decelerating
    t_cold = time.time()
    await controller.start()
    await asyncio.sleep(3.0)
    a = await attempt_set_speed(controller, ts, 2.5, "E4_stop_start_setspeed", rep, t_cold)
    await safe_full_stop(controller)
    return a if isinstance(a, list) else [a]


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--reps", type=int, default=2,
                   help="repetitions per experiment")
    p.add_argument("--out", type=Path, default=Path("/tmp/preworkout_results.tsv"))
    p.add_argument("--log", default="WARNING")
    args = p.parse_args()
    setup_logging(args.log)

    all_attempts: list[Attempt] = []

    for exp_fn, exp_name in [
        (experiment_after_reset, "E1_after_reset"),
        (experiment_cold_setspeed_repeated, "E2_cold_repeated"),
        (experiment_set_inclination, "E3_set_incline"),
        (experiment_stop_start_setspeed, "E4_stop_start_setspeed"),
    ]:
        for rep in range(1, args.reps + 1):
            controller = await connect(name=args.name)
            if controller is None:
                _LOGGER.error("connect failed; skipping %s rep %d", exp_name, rep)
                continue
            ts = TraceState()
            await setup_trace(controller, ts)
            try:
                attempts = await exp_fn(controller, ts, rep)
                all_attempts.extend(attempts)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("%s rep %d raised: %s", exp_name, rep, err)
            finally:
                await teardown_trace(ts)
                await safe_disconnect(controller)
            await asyncio.sleep(5.0)  # let device settle between experiments

    # Write TSV
    with open(args.out, "w") as f:
        f.write("experiment\trep\ttarget\ttrain_status\tfm_event\tspeed_before\tspeed_after\tcmd_ok\tactually_changed\telapsed_s\textra\n")
        for a in all_attempts:
            target = a.target_speed if a.target_speed is not None else f"incline:{a.target_incline}"
            f.write(
                f"{a.experiment}\t{a.rep}\t{target}\t{a.training_status_at_write}\t"
                f"{a.last_fm_event}\t{a.speed_before:.2f}\t{a.speed_after:.2f}\t"
                f"{a.cmd_ok}\t{a.actually_changed}\t{a.elapsed_since_start_s:.1f}\t{a.extra}\n"
            )

    # Falsification analysis
    print()
    print("=" * 100)
    print("FALSIFICATION ANALYSIS")
    print("=" * 100)
    print(f"{'experiment':<28} {'rep':>4} {'target':>10} {'train':>6} {'fm_ev':>6} "
          f"{'before':>7} {'after':>7} {'cmd_ok':>7} {'changed':>8} {'elapsed':>9}")
    print("-" * 100)
    for a in all_attempts:
        target = (f"{a.target_speed:.2f}" if a.target_speed is not None else f"i:{a.target_incline}")
        print(f"{a.experiment:<28} {a.rep:>4} {target:>10} "
              f"{a.training_status_at_write!s:>6} {a.last_fm_event!s:>6} "
              f"{a.speed_before:>7.2f} {a.speed_after:>7.2f} "
              f"{str(a.cmd_ok):>7} {str(a.actually_changed):>8} {a.elapsed_since_start_s:>9.1f}")

    speed_attempts = [a for a in all_attempts if a.target_speed is not None]
    if not speed_attempts:
        return

    in_pw = [a for a in speed_attempts if a.training_status_at_write == 13]
    out_pw = [a for a in speed_attempts if a.training_status_at_write is not None and a.training_status_at_write != 13]
    unknown = [a for a in speed_attempts if a.training_status_at_write is None]

    print()
    print("Hypothesis: training_status == 13 (PRE_WORKOUT) blocks SET_TARGET_SPEED")
    print("-" * 100)
    print(f"  set_speed in PRE_WORKOUT: {len(in_pw)} attempts, "
          f"{sum(1 for a in in_pw if a.actually_changed)} actually changed speed")
    print(f"  set_speed not in PRE_WORKOUT (status known): {len(out_pw)} attempts, "
          f"{sum(1 for a in out_pw if a.actually_changed)} actually changed speed")
    print(f"  set_speed with unknown training_status: {len(unknown)} attempts, "
          f"{sum(1 for a in unknown if a.actually_changed)} actually changed")
    print()

    fal1 = any(a.actually_changed for a in in_pw)
    fal2 = any(not a.actually_changed for a in out_pw)
    if fal1:
        print("  FALSIFIED: at least one set_speed succeeded while in PRE_WORKOUT.")
    if fal2:
        print("  FALSIFIED: at least one set_speed failed while NOT in PRE_WORKOUT.")
    if not fal1 and not fal2 and (in_pw or out_pw):
        print("  Hypothesis NOT falsified by this run.")


if __name__ == "__main__":
    asyncio.run(main())
