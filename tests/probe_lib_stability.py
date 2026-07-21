"""How often does WalkingPadController.connect() + idle survive?

Runs N reps of: connect → idle → disconnect, on a fresh controller
each time. Measures how long the connection actually holds.

This is the headline metric — if the connect-path trim worked, the
held times should be much closer to the bare-BleakClient baseline
(reliably ≥ idle_seconds).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from commands._common import DEFAULT_DEVICE_NAME, connect, safe_disconnect, setup_logging  # noqa: E402

_LOGGER = logging.getLogger("stability")


async def run_one(idle: float, name: str) -> tuple[bool, float]:
    """Returns (survived, held_seconds)."""
    disc_at = [None]

    def disc():
        disc_at[0] = time.time()

    controller = await connect(name=name, on_disconnect=disc)
    if controller is None:
        return False, 0.0
    t_connected = time.time()
    deadline = t_connected + idle
    while time.time() < deadline and disc_at[0] is None:
        await asyncio.sleep(0.2)
    survived = disc_at[0] is None
    held = (disc_at[0] - t_connected) if disc_at[0] else (time.time() - t_connected)
    await safe_disconnect(controller)
    return survived, held


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--idle", type=float, default=20.0)
    p.add_argument("--gap", type=float, default=6.0)
    p.add_argument("--log", default="WARNING")
    args = p.parse_args()
    setup_logging(args.log)

    survived = 0
    helds: list[float] = []
    for rep in range(1, args.reps + 1):
        s, h = await run_one(args.idle, args.name)
        helds.append(h)
        flag = "OK" if s else "DISC"
        print(f"  rep {rep}: {flag} held={h:.1f}s")
        if s:
            survived += 1
        await asyncio.sleep(args.gap)

    print()
    print(f"Survived {survived}/{args.reps} idle-{args.idle:.0f}s windows")
    if helds:
        print(f"Held seconds: min={min(helds):.1f}  avg={sum(helds)/len(helds):.1f}  max={max(helds):.1f}")


if __name__ == "__main__":
    asyncio.run(main())
