"""set_speed — set a single target speed (default 2.0 km/h) or sweep."""

import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    # Belt must be moving for set_target_speed to apply on KingSmith firmware.
    if controller.status.speed == 0 and args.start_first:
        await controller.start()
        await asyncio.sleep(args.settle)

    if args.sweep:
        results = []
        for v in args.sweep:
            ok = await controller.set_speed(v)
            await asyncio.sleep(args.gap)
            results.append({"target": v, "ok": ok, "observed": controller.status.speed})
        all_ok = all(r["ok"] for r in results)
        return {"ok": all_ok, "results": results}

    ok = await controller.set_speed(args.speed)
    await asyncio.sleep(args.gap)
    return {
        "ok": ok,
        "target": args.speed,
        "observed": controller.status.speed,
    }


def add_args(p):
    p.add_argument("--speed", type=float, default=2.0,
                   help="target speed in km/h (single value)")
    p.add_argument("--sweep", type=float, nargs="*",
                   help="sweep multiple target speeds (overrides --speed)")
    p.add_argument("--start-first", action="store_true",
                   help="cold-start belt first if stopped")
    p.add_argument("--settle", type=float, default=4.0,
                   help="settle time after start before set_speed")
    p.add_argument("--gap", type=float, default=2.0,
                   help="seconds to wait after set_speed before reading")


if __name__ == "__main__":
    sys.exit(run_module("set_speed", fn, add_args))
