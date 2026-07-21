"""Shared test helpers for the per-command scripts and the orchestrator.

Goals:
  - Each command lives in its own file; they all use the same envelope so
    the orchestrator can call them uniformly.
  - Single connect-with-retry implementation (the device is BLE-flaky;
    retrying is mandatory in the real world).
  - Structured result dict per run, suitable for both stdout JSON and
    in-process orchestration.

Usage:

    from tests.commands._common import find_device, connect, safe_disconnect, run_module

    # standalone
    if __name__ == "__main__":
        run_module(do_command)

    async def do_command(controller, args) -> dict:
        ok = await controller.start()
        return {"ok": ok}
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from bleak import BleakScanner

from walkingpad_controller import WalkingPadController, TreadmillStatus

DEFAULT_DEVICE_NAME = "KS-HD-Z1D"
DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_CONNECT_ATTEMPTS = 3
DEFAULT_RETRY_GAP = 4.0


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("bleak").setLevel(logging.WARNING)


@dataclass
class CommandResult:
    """Standard result envelope every command script returns."""

    command: str
    ok: bool = False
    latency_ms: float = 0.0
    error: str | None = None
    before_connected: bool | None = None
    after_connected: bool | None = None
    before_speed: float | None = None
    after_speed: float | None = None
    before_belt_state: int | None = None
    after_belt_state: int | None = None
    args: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def as_json(self) -> str:
        return json.dumps(asdict(self), default=str)


async def find_device(name: str = DEFAULT_DEVICE_NAME, timeout: float = DEFAULT_CONNECT_TIMEOUT):
    """Locate the device by advertised name. Returns a BLEDevice or None."""
    return await BleakScanner.find_device_by_name(name, timeout=timeout)


async def connect(
    name: str = DEFAULT_DEVICE_NAME,
    attempts: int = DEFAULT_CONNECT_ATTEMPTS,
    retry_gap: float = DEFAULT_RETRY_GAP,
    on_status=None,
    on_disconnect=None,
) -> WalkingPadController | None:
    """Scan + connect with retry. Returns a connected controller or None."""
    log = logging.getLogger("connect")
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            device = await find_device(name)
            if device is None:
                log.warning("attempt %d: device %r not found", attempt, name)
                last_err = RuntimeError(f"device {name!r} not advertising")
            else:
                controller = WalkingPadController(ble_device=device)
                if on_status:
                    controller.register_status_callback(on_status)
                if on_disconnect:
                    controller.register_disconnect_callback(on_disconnect)
                await controller.connect()
                if controller.connected:
                    return controller
                log.warning("attempt %d: connect returned but not connected", attempt)
                last_err = RuntimeError("connect: not connected after return")
        except Exception as err:  # noqa: BLE001
            log.warning("attempt %d: %s", attempt, err)
            last_err = err
        if attempt < attempts:
            await asyncio.sleep(retry_gap)
    log.error("all %d attempts failed; last error: %s", attempts, last_err)
    return None


async def safe_disconnect(controller: WalkingPadController | None) -> None:
    if controller is None:
        return
    with contextlib.suppress(Exception):
        if controller.connected:
            await controller.disconnect()


def snapshot(controller: WalkingPadController | None) -> dict:
    """Capture {connected, speed, belt_state} for before/after comparison."""
    if controller is None:
        return {"connected": False, "speed": None, "belt_state": None}
    s: TreadmillStatus = controller.status
    return {
        "connected": controller.connected,
        "speed": s.speed,
        "belt_state": s.belt_state,
    }


async def run_inprocess(
    command: str,
    fn: Callable[[WalkingPadController, argparse.Namespace], Awaitable[dict]],
    args: argparse.Namespace,
    keep_open: bool = False,
    controller: WalkingPadController | None = None,
) -> tuple[CommandResult, WalkingPadController | None]:
    """Connect (or reuse), run fn, disconnect (unless keep_open). Used by the
    orchestrator when running multiple commands on the same connection."""

    own_connection = controller is None
    if own_connection:
        controller = await connect(name=args.name)

    result = CommandResult(command=command, args=vars(args))
    before = snapshot(controller)
    result.before_connected = before["connected"]
    result.before_speed = before["speed"]
    result.before_belt_state = before["belt_state"]

    if controller is None:
        result.ok = False
        result.error = "could-not-connect"
        return result, None

    t0 = time.time()
    try:
        extra = await fn(controller, args)
        result.ok = bool(extra.get("ok", True))
        result.extra = {k: v for k, v in extra.items() if k != "ok"}
        if "error" in extra:
            result.error = str(extra["error"])
    except Exception as err:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(err).__name__}: {err}"
    result.latency_ms = (time.time() - t0) * 1000.0

    after = snapshot(controller)
    result.after_connected = after["connected"]
    result.after_speed = after["speed"]
    result.after_belt_state = after["belt_state"]

    if own_connection and not keep_open:
        await safe_disconnect(controller)
        controller = None

    return result, controller


def base_argparser(command: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f"walkingpad-controller test: {command}")
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME, help="BLE device name")
    p.add_argument("--log", default="INFO", help="log level")
    p.add_argument("--json", action="store_true", help="emit JSON result and nothing else on stdout")
    return p


def run_module(
    command: str,
    fn: Callable[[WalkingPadController, argparse.Namespace], Awaitable[dict]],
    extra_args: Callable[[argparse.ArgumentParser], None] | None = None,
) -> int:
    """Boilerplate for a standalone command script.

    The script defines an `async fn(controller, args)` and calls
    `run_module("name", fn)`. We handle CLI parsing, connect, run, disconnect.
    """
    parser = base_argparser(command)
    if extra_args:
        extra_args(parser)
    args = parser.parse_args()
    setup_logging(args.log)

    async def main():
        result, _ = await run_inprocess(command, fn, args)
        if args.json:
            print(result.as_json())
        else:
            print(
                f"[{result.command}] ok={result.ok} latency={result.latency_ms:.0f}ms "
                f"connected: {result.before_connected}->{result.after_connected} "
                f"speed: {result.before_speed}->{result.after_speed} "
                f"belt: {result.before_belt_state}->{result.after_belt_state} "
                f"err={result.error} extra={result.extra}"
            )
        return 0 if result.ok else 1

    return asyncio.run(main())
