"""switch_mode — exercise the OperatingMode (AUTO/MANUAL/STANDBY) path."""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from walkingpad_controller import OperatingMode  # noqa: E402

from commands._common import run_module  # noqa: E402


_NAMED = {m.name.lower(): m for m in OperatingMode}


async def fn(controller, args) -> dict:
    mode = _NAMED.get(args.mode.lower())
    if mode is None:
        return {"ok": False, "error": f"unknown mode: {args.mode!r}"}
    ok = await controller.switch_mode(mode)
    return {"ok": ok, "requested": mode.name}


def add_args(p):
    p.add_argument("--mode", default="standby",
                   choices=sorted(_NAMED), help="target OperatingMode")


if __name__ == "__main__":
    sys.exit(run_module("switch_mode", fn, add_args))
