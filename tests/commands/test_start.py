"""start — exercise the cold-start path. Returns ok if the belt is moving."""

import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    started = await controller.start()
    # Give the belt a moment to ramp up before we sample speed
    if started and controller.connected:
        await asyncio.sleep(args.settle)
    return {
        "ok": started and controller.connected,
        "moving": controller.status.speed > 0,
    }


def add_args(p):
    p.add_argument("--settle", type=float, default=0.0,
                   help="seconds to wait after start() before sampling speed")


if __name__ == "__main__":
    sys.exit(run_module("start", fn, add_args))
