"""pause — sends FTMS STOP_OR_PAUSE with PAUSE param via the public API.

Distinct from stop in that the session counters (time/distance/cal/steps)
survive the pause and a subsequent start() resumes them. Verifies that
once the belt has decelerated to zero, belt_state lands at PAUSED — the
distinguishing observable from a hard stop, which lands at STOPPED.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from walkingpad_controller import BeltState  # noqa: E402

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    if not controller.connected:
        return {"ok": False, "error": "not connected"}

    # Optionally bring the belt up first so we have something to pause
    if args.start_first and controller.status.speed == 0:
        started = await controller.start()
        if not started:
            return {"ok": False, "error": "start_first failed"}
        # Let speed climb a bit so the deceleration window after pause
        # is observable
        await asyncio.sleep(args.run_for)

    speed_before = controller.status.speed
    ok = await controller.pause()
    if not ok:
        return {
            "ok": False, "error": "pause() returned False",
            "speed_before": speed_before,
        }

    # Wait until the belt actually reaches zero (deceleration takes ~5-10s
    # on KS-HD-Z1D from 2.5 km/h). Cap at args.timeout.
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not controller.connected:
            return {
                "ok": False, "error": "disconnected during deceleration",
                "speed_before": speed_before,
            }
        if controller.status.speed < 0.05:
            break
        await asyncio.sleep(0.2)

    final_state = int(controller.status.belt_state)
    final_speed = controller.status.speed
    return {
        "ok": final_state == BeltState.PAUSED,
        "speed_before": speed_before,
        "speed_after": final_speed,
        "belt_state": final_state,
        "belt_state_name": BeltState(final_state).name if final_state in iter(BeltState) else "?",
        "duration_s": controller.status.duration,
    }


def add_args(p):
    p.add_argument("--start-first", action="store_true",
                   help="cold-start the belt first if it isn't moving")
    p.add_argument("--run-for", type=float, default=3.0,
                   help="seconds to leave belt running before pause (when --start-first)")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="seconds to wait for speed to reach zero before giving up")


if __name__ == "__main__":
    sys.exit(run_module("pause", fn, add_args))
