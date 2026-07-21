"""stop — must be issued only when the belt is running, otherwise the
device may reject it. Optionally starts the belt first via --start-first."""

import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    started = None
    if args.start_first and controller.status.speed == 0:
        started = await controller.start()
        await asyncio.sleep(args.run_for)
    stopped = await controller.stop()
    return {
        "ok": stopped,
        "pre_started": started,
    }


def add_args(p):
    p.add_argument("--start-first", action="store_true",
                   help="cold-start the belt first if it isn't moving")
    p.add_argument("--run-for", type=float, default=2.0,
                   help="seconds to leave belt running before stop (when --start-first)")


if __name__ == "__main__":
    sys.exit(run_module("stop", fn, add_args))
