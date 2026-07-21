"""connect — exercise the full connect flow (scan + BleakClient + setup)."""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from commands._common import run_module  # noqa: E402


async def fn(controller, args) -> dict:
    # If we got here, _common.run_inprocess already connected. Just verify.
    return {
        "ok": controller.connected,
        "protocol": controller.protocol.value,
        "min_speed": controller.min_speed,
        "max_speed": controller.max_speed,
        "speed_increment": controller.speed_increment,
        "firmware_version": controller.firmware_version,
    }


if __name__ == "__main__":
    sys.exit(run_module("connect", fn))
