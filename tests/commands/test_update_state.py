"""update_state — fire a status refresh; on FTMS this is a synthetic update."""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    await controller.update_state()
    s = controller.status
    return {
        "ok": True,
        "speed": s.speed,
        "belt_state": int(s.belt_state),
        "duration_s": s.duration,
        "distance_m": s.distance,
        "calories": s.calories,
        "steps": s.steps,
        "training_status": s.training_status,
        "last_fm_event": s.last_fm_event,
    }


if __name__ == "__main__":
    sys.exit(run_module("update_state", fn))
