"""resume — call start() against a paused belt and verify the session
continues (counters not reset, belt_state ACTIVE).

Functionally identical to test_start.py at the BLE layer (FTMS uses one
opcode for both — `START_OR_RESUME`), but distinguished by the expected
observable: counters carry over rather than start at zero.

Useful as a discrete script for the orchestrator's pause→resume scenario,
where we want to verify the resume specifically preserved session state.
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

    pre_state = int(controller.status.belt_state)
    pre_duration = controller.status.duration
    pre_distance = controller.status.distance
    pre_steps = controller.status.steps

    started = await controller.start()
    if not started:
        return {
            "ok": False, "error": "start() returned False",
            "pre_state": pre_state,
        }

    # Wait for belt_state == ACTIVE (or timeout)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not controller.connected:
            return {"ok": False, "error": "disconnected during resume"}
        if int(controller.status.belt_state) == BeltState.ACTIVE:
            break
        await asyncio.sleep(0.2)

    post_state = int(controller.status.belt_state)
    return {
        "ok": post_state == BeltState.ACTIVE,
        "pre_state": pre_state,
        "post_state": post_state,
        "pre_state_name": BeltState(pre_state).name if pre_state in iter(BeltState) else "?",
        "post_state_name": BeltState(post_state).name if post_state in iter(BeltState) else "?",
        # Session counters before/after — orchestrator checks they don't
        # regress (which would mean the device treated this as a fresh
        # session rather than a resume).
        "pre_duration": pre_duration,
        "post_duration": controller.status.duration,
        "pre_distance": pre_distance,
        "post_distance": controller.status.distance,
        "pre_steps": pre_steps,
        "post_steps": controller.status.steps,
    }


def add_args(p):
    p.add_argument("--timeout", type=float, default=10.0,
                   help="seconds to wait for belt to reach ACTIVE")


if __name__ == "__main__":
    sys.exit(run_module("resume", fn, add_args))
