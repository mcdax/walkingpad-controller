"""set_target_inclination — FTMS opcode 0x03. Most KingSmith treadmills
have fixed inclination so this typically returns OPERATION_FAILED, but
the test exists to confirm graceful handling."""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    if controller._ftms is None:
        return {"ok": False, "error": "no FTMS backend (WiLink device?)"}
    ok = await controller._ftms.set_target_inclination(args.percent)
    return {"ok": ok, "target_percent": args.percent}


def add_args(p):
    p.add_argument("--percent", type=float, default=0.0,
                   help="inclination in percent")


if __name__ == "__main__":
    sys.exit(run_module("set_target_inclination", fn, add_args))
