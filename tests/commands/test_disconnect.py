"""disconnect — connect, then disconnect cleanly. Verifies post-state."""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    await controller.disconnect()
    return {"ok": not controller.connected}


if __name__ == "__main__":
    sys.exit(run_module("disconnect", fn))
