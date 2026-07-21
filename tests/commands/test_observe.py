"""observe — sit idle on an existing connection for N seconds and snapshot
the resulting status. No commands sent.

Used by the orchestrator to insert observation windows between commands
(e.g. "verify counters keep advancing during a 5-second walk window").
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    if not controller.connected:
        return {"ok": False, "error": "not connected"}

    pre = {
        "speed": controller.status.speed,
        "belt_state": int(controller.status.belt_state),
        "duration": controller.status.duration,
        "distance": controller.status.distance,
        "steps": controller.status.steps,
        "calories": controller.status.calories,
    }

    deadline = time.time() + args.duration
    samples = 0
    last_speed = pre["speed"]
    speed_min, speed_max = pre["speed"], pre["speed"]
    while time.time() < deadline:
        if not controller.connected:
            return {"ok": False, "error": "disconnected during observe", "pre": pre}
        s = controller.status.speed
        speed_min = min(speed_min, s)
        speed_max = max(speed_max, s)
        last_speed = s
        samples += 1
        await asyncio.sleep(0.2)

    post = {
        "speed": last_speed,
        "belt_state": int(controller.status.belt_state),
        "duration": controller.status.duration,
        "distance": controller.status.distance,
        "steps": controller.status.steps,
        "calories": controller.status.calories,
    }
    return {
        "ok": True,
        "pre": pre,
        "post": post,
        "samples": samples,
        "speed_min": speed_min,
        "speed_max": speed_max,
    }


def add_args(p):
    p.add_argument("--duration", type=float, default=3.0,
                   help="seconds to observe")


if __name__ == "__main__":
    sys.exit(run_module("observe", fn, add_args))
