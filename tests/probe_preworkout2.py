"""Targeted probe — wait for a specific training_status, then test set_speed.

Pinpoints whether PRE_WORKOUT specifically blocks SET_TARGET_SPEED, or
whether the failures are correlated with something else entirely.

For each rep:
  1. reset + start (cold)
  2. wait up to 15 s for training_status to reach 13 (PRE_WORKOUT) — the
     lib's _on_training_status puts it on controller.status.training_status
  3. snapshot (status, speed) and write SET_TARGET_SPEED(2.0)
  4. wait up to 5 s for either:
        - speed to physically change to ~2.0 (success)
        - any change in training_status
        - or 5 s elapse (timeout)
  5. record outcome and what happened

Also captures the full sequence of training_status / fm_status / speed
changes for forensic review.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from commands._common import DEFAULT_DEVICE_NAME, connect, safe_disconnect, setup_logging  # noqa: E402

_LOGGER = logging.getLogger("preworkout2")


TRAINING_STATUS_NAME = {
    0: "OTHER", 1: "IDLE", 2: "WARMING_UP",
    3: "LOW_INT", 4: "HIGH_INT", 5: "RECOVERY",
    6: "ISOMETRIC", 7: "HR_CONTROL", 8: "FITNESS_TEST",
    9: "SPEED_OOC", 10: "COOL_DOWN", 11: "WATT_CONTROL",
    12: "MANUAL", 13: "PRE_WORKOUT", 14: "POST_WORKOUT",
}


def ts_name(s: int | None) -> str:
    if s is None:
        return "?"
    return TRAINING_STATUS_NAME.get(s, f"0x{s:02x}")


@dataclass
class Sample:
    t: float
    training_status: int
    last_fm_event: int
    speed: float


@dataclass
class Result:
    rep: int
    sequence: str
    target: float
    pre_set_speed_status: int
    pre_set_speed_fm: int
    pre_set_speed_speed: float
    post_set_speed_status: int
    post_set_speed_fm: int
    post_set_speed_speed: float
    cmd_ok: bool
    speed_changed: bool
    transitions: list[Sample] = field(default_factory=list)


async def watch_status(controller, until: callable, timeout: float, samples: list[Sample]):
    """Poll controller.status until `until(status)` returns True or timeout."""
    deadline = time.time() + timeout
    last_ts = -999
    last_fm = -999
    last_sp = -999.0
    while time.time() < deadline:
        s = controller.status
        if s.training_status != last_ts or s.last_fm_event != last_fm or abs(s.speed - last_sp) > 0.01:
            samples.append(Sample(
                t=time.time(),
                training_status=s.training_status,
                last_fm_event=s.last_fm_event,
                speed=s.speed,
            ))
            last_ts = s.training_status
            last_fm = s.last_fm_event
            last_sp = s.speed
        if until(s):
            return True
        await asyncio.sleep(0.1)
    return False


async def run_rep(controller, rep: int, target: float = 2.0) -> Result:
    samples: list[Sample] = []

    # Force-rewrap the training_status handler so we KNOW updates flow into
    # controller.status.  In some Bleak/BlueZ orderings, a stale subscription
    # from a prior process can shadow the new one — re-binding here makes
    # this probe deterministic regardless.
    client = controller._ftms._client
    orig_ts_handler = controller._ftms._on_training_status
    with contextlib.suppress(Exception):
        await client.stop_notify("00002ad3-0000-1000-8000-00805f9b34fb")
    with contextlib.suppress(Exception):
        await client.start_notify(
            "00002ad3-0000-1000-8000-00805f9b34fb",
            lambda h, d: orig_ts_handler(h, bytearray(d)),
        )

    # Reset + cold start
    await controller._ftms.reset()
    await asyncio.sleep(1.0)
    await controller.start()

    # Wait up to 15s for training_status==13 (PRE_WORKOUT)
    reached_pre = await watch_status(
        controller,
        until=lambda s: s.training_status == 13,
        timeout=15.0,
        samples=samples,
    )
    _LOGGER.info("rep %d: waited for PRE_WORKOUT, reached=%s, current=%s",
                 rep, reached_pre, ts_name(controller.status.training_status))

    # Capture pre-state
    s_pre = controller.status
    pre_status = s_pre.training_status
    pre_fm = s_pre.last_fm_event
    pre_speed = s_pre.speed

    # Send SET_TARGET_SPEED. The library logs the result code if any.
    cmd_ok = await controller.set_speed(target)
    _LOGGER.info("rep %d: set_speed(%.1f) returned %s", rep, target, cmd_ok)

    # Watch for speed change for up to 5s after the command
    await watch_status(
        controller,
        until=lambda s: abs(s.speed - target) < 0.3,
        timeout=5.0,
        samples=samples,
    )

    s_post = controller.status
    speed_changed = abs(s_post.speed - target) < 0.3
    return Result(
        rep=rep,
        sequence="reset_start_setspeed",
        target=target,
        pre_set_speed_status=pre_status,
        pre_set_speed_fm=pre_fm,
        pre_set_speed_speed=pre_speed,
        post_set_speed_status=s_post.training_status,
        post_set_speed_fm=s_post.last_fm_event,
        post_set_speed_speed=s_post.speed,
        cmd_ok=cmd_ok,
        speed_changed=speed_changed,
        transitions=samples,
    )


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--log", default="WARNING")
    args = p.parse_args()
    setup_logging(args.log)

    results: list[Result] = []
    for rep in range(1, args.reps + 1):
        controller = await connect(name=args.name)
        if controller is None:
            _LOGGER.error("rep %d connect failed", rep)
            continue
        try:
            r = await run_rep(controller, rep)
            results.append(r)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("rep %d raised: %s", rep, err)
        finally:
            with contextlib.suppress(Exception):
                if controller.connected and controller.status.speed > 0:
                    await controller.stop()
            await safe_disconnect(controller)
        await asyncio.sleep(8.0)

    print()
    print("=" * 90)
    print("RESULTS")
    print("=" * 90)
    print(f"{'rep':>3} {'pre_status':<14} {'pre_fm':<8} {'pre_sp':>7} "
          f"{'post_status':<14} {'post_fm':<8} {'post_sp':>7} "
          f"{'cmd_ok':>7} {'changed':>8}")
    for r in results:
        print(f"{r.rep:>3} {ts_name(r.pre_set_speed_status):<14} "
              f"0x{r.pre_set_speed_fm:02x}     {r.pre_set_speed_speed:>7.2f} "
              f"{ts_name(r.post_set_speed_status):<14} "
              f"0x{r.post_set_speed_fm:02x}     {r.post_set_speed_speed:>7.2f} "
              f"{str(r.cmd_ok):>7} {str(r.speed_changed):>8}")

    print()
    print("=" * 90)
    print("PER-REP TRANSITIONS")
    print("=" * 90)
    for r in results:
        print(f"--- rep {r.rep} ---")
        if not r.transitions:
            print("  (no samples)")
            continue
        t0 = r.transitions[0].t
        for s in r.transitions:
            print(f"  +{s.t - t0:5.2f}s  status={ts_name(s.training_status):<13} "
                  f"fm=0x{s.last_fm_event:02x}  speed={s.speed:.2f}")
        print()

    # Final verdict
    pre_attempts = [r for r in results if r.pre_set_speed_status == 13]
    print("=" * 90)
    print("VERDICT")
    print("=" * 90)
    print(f"  attempts in PRE_WORKOUT (status=13): {len(pre_attempts)}")
    if pre_attempts:
        ok_count = sum(1 for r in pre_attempts if r.speed_changed)
        print(f"  speed actually changed: {ok_count}/{len(pre_attempts)}")
        if ok_count == 0 and len(pre_attempts) >= 2:
            print("  HYPOTHESIS HOLDS: PRE_WORKOUT consistently blocks set_speed")
        elif ok_count == len(pre_attempts):
            print("  HYPOTHESIS FALSIFIED: set_speed always worked in PRE_WORKOUT")
        else:
            print("  PARTIAL: outcome inconsistent in PRE_WORKOUT — other factor at play")


if __name__ == "__main__":
    asyncio.run(main())
