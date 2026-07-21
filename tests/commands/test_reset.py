"""reset — FTMS RESET (opcode 0x01). Some firmwares don't support this."""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    if controller._ftms is None:
        return {"ok": False, "error": "no FTMS backend (WiLink device?)"}
    ok = await controller._ftms.reset()
    return {"ok": ok}


if __name__ == "__main__":
    sys.exit(run_module("reset", fn))
