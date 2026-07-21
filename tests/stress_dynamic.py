"""Dynamic stress test driving the per-command scripts.

Imports the command modules in tests/commands/ and runs them in random
or weighted sequences against a single live connection (with reconnect
on disconnect).  Each command reports through the shared CommandResult
envelope; the orchestrator aggregates outcomes by command, by sequence
position, and by transition (cmd_a -> cmd_b) so we can spot patterns
like "set_speed after stop nearly always fails."

Use cases:
  - Random walk:       pick commands at random for N iterations
  - Scenario:          predefined sequences (e.g. start->set_speed->stop)
                       repeated to surface intermittent failures
  - Soak:              hold a connection for a long time, occasionally
                       firing low-impact commands

Output: a human summary on stderr/stdout, plus a per-step JSONL log if
--jsonl is given.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from commands._common import (  # noqa: E402
    DEFAULT_DEVICE_NAME,
    CommandResult,
    connect,
    safe_disconnect,
    setup_logging,
    snapshot,
)

from commands import test_connect  # noqa: E402,F401  (registers command)
from commands import test_disconnect, test_pause, test_reset  # noqa: E402
from commands import (  # noqa: E402
    test_observe,
    test_resume,
    test_set_inclination,
    test_set_speed,
    test_start,
    test_stop,
    test_switch_mode,
    test_update_state,
)

from walkingpad_controller import BeltState  # noqa: E402

_LOGGER = logging.getLogger("orchestrator")


# Map command name -> (module-level fn, default args namespace builder)
def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(name=DEFAULT_DEVICE_NAME, log="WARNING", json=False, **kw)


COMMANDS: dict[str, tuple[Callable, Callable[[], argparse.Namespace]]] = {
    "start":              (test_start.fn,            lambda: _ns(settle=2.0)),
    "resume":             (test_resume.fn,           lambda: _ns(timeout=10.0)),
    "stop":               (test_stop.fn,             lambda: _ns(start_first=False, run_for=0.0)),
    "set_speed":          (test_set_speed.fn,        lambda: _ns(
                                speed=random.choice([1.5, 2.0, 2.5, 3.0, 3.5, 4.0]),
                                sweep=None, start_first=False, settle=0, gap=1.0)),
    "switch_mode":        (test_switch_mode.fn,      lambda: _ns(
                                mode=random.choice(["manual", "standby", "auto"]))),
    "update_state":       (test_update_state.fn,     lambda: _ns()),
    "pause":              (test_pause.fn,            lambda: _ns(
                                start_first=False, run_for=0.0, timeout=15.0)),
    "reset":              (test_reset.fn,            lambda: _ns()),
    "set_inclination":    (test_set_inclination.fn,  lambda: _ns(percent=0.0)),
    "observe":            (test_observe.fn,          lambda: _ns(duration=3.0)),
}


# Default weights — bias toward common operations; expensive/rejection-prone
# commands (reset, pause, set_inclination) get lower weights.
DEFAULT_WEIGHTS = {
    "start":           4,
    "stop":            3,
    "set_speed":       4,
    "switch_mode":     1,
    "update_state":    3,
    "pause":           1,
    "reset":           1,
    "set_inclination": 1,
}


SCENARIOS: dict[str, list[str]] = {
    "happy":           ["start", "set_speed", "set_speed", "set_speed", "stop"],
    "rapid_speed":     ["start", "set_speed", "set_speed", "set_speed", "set_speed", "set_speed", "stop"],
    "stop_start":      ["start", "set_speed", "stop", "start", "set_speed", "stop"],
    "pause_resume":    ["start", "set_speed", "observe", "pause", "resume",
                        "observe", "set_speed", "stop"],
    "pause_then_stop": ["start", "set_speed", "pause", "stop"],
    "hard_stop_reset": ["start", "set_speed", "observe", "stop", "start",
                        "observe"],
    "spam_state":      ["start", "update_state", "update_state", "update_state",
                        "set_speed", "update_state", "stop"],
    "ftms_corner":     ["set_inclination", "reset", "switch_mode", "update_state"],
}


# Per-scenario semantic invariants — checked after the scenario runs.
# Each invariant takes the list of StepRecord and returns a list of
# violation messages (empty = passed). Run on a clean session against
# the live device.
def _check_pause_resume(records: list) -> list[str]:
    """pause_resume: belt_state should reach PAUSED after a successful pause;
    counters should monotonically increase across the resume (not regress
    to 0). Pauses that failed at the BLE level (`ok = False`) are already
    accounted for in PER-COMMAND OUTCOMES — don't double-flag them here.
    """
    issues = []
    pause_recs = [r for r in records if r.cmd == "pause" and r.ok]
    resume_recs = [r for r in records if r.cmd == "resume" and r.ok]
    for r in pause_recs:
        bs = r.extra.get("belt_state")
        if bs != BeltState.PAUSED:
            issues.append(
                f"pause didn't land at PAUSED: belt_state={bs} "
                f"({r.extra.get('belt_state_name', '?')})"
            )
    for r in resume_recs:
        pre = r.extra.get("pre_duration", 0)
        post = r.extra.get("post_duration", 0)
        if post < pre:
            issues.append(
                f"resume regressed duration counter: pre={pre} post={post} "
                "(treated as fresh session, not a resume)"
            )
        # Same check on distance + steps (pre-stop counters may be 0
        # because the device only increments on detected weight, but
        # they shouldn't go backwards either)
        if r.extra.get("post_distance", 0) < r.extra.get("pre_distance", 0):
            issues.append("resume regressed distance counter")
        if r.extra.get("post_steps", 0) < r.extra.get("pre_steps", 0):
            issues.append("resume regressed steps counter")
    return issues


def _check_hard_stop_reset(records: list) -> list[str]:
    """hard_stop_reset: after stop+start, the second observe should show
    counters near zero (session reset, not continuing)."""
    issues = []
    observes = [r for r in records if r.cmd == "observe"]
    if len(observes) < 2:
        return issues
    pre_stop = observes[0].extra.get("post", {})
    post_restart = observes[1].extra.get("post", {})
    pre_dur = pre_stop.get("duration", 0)
    post_dur = post_restart.get("duration", 0)
    if post_dur > pre_dur + 5:
        # Allow a few seconds of slack — the new session has been
        # running by the time the second observe happens
        issues.append(
            f"hard stop+start didn't reset duration: pre_stop={pre_dur}s, "
            f"post_restart={post_dur}s — session continued instead of reset"
        )
    return issues


SCENARIO_CHECKS: dict[str, Callable[[list], list[str]]] = {
    "pause_resume":    _check_pause_resume,
    "pause_then_stop": _check_pause_resume,
    "hard_stop_reset": _check_hard_stop_reset,
}


@dataclass
class StepRecord:
    iter: int
    cmd: str
    ok: bool
    latency_ms: float
    error: str | None
    before_connected: bool | None
    after_connected: bool | None
    before_speed: float | None
    after_speed: float | None
    extra: dict


async def _run_step(controller, cmd: str, args, iteration: int) -> tuple[StepRecord, bool]:
    """Run one command on an existing controller. Returns (record, still_connected)."""
    fn, _ = COMMANDS[cmd]
    before = snapshot(controller)
    t0 = time.time()
    error: str | None = None
    extra: dict = {}
    try:
        extra = await fn(controller, args)
        ok = bool(extra.get("ok", True))
    except Exception as err:  # noqa: BLE001
        ok = False
        error = f"{type(err).__name__}: {err}"
    latency = (time.time() - t0) * 1000.0
    after = snapshot(controller)

    rec = StepRecord(
        iter=iteration,
        cmd=cmd,
        ok=ok,
        latency_ms=latency,
        error=error,
        before_connected=before["connected"],
        after_connected=after["connected"],
        before_speed=before["speed"],
        after_speed=after["speed"],
        extra={k: v for k, v in extra.items() if k != "ok"},
    )
    return rec, after["connected"]


async def main():
    p = argparse.ArgumentParser(description="Dynamic stress orchestrator")
    p.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    p.add_argument("--mode", choices=["random", "scenario", "soak"], default="random")
    p.add_argument("--scenario", choices=list(SCENARIOS), default="happy")
    p.add_argument("--iterations", type=int, default=20,
                   help="random mode: number of commands; scenario mode: scenario repetitions")
    p.add_argument("--inter-cmd-gap", type=float, default=2.0,
                   help="seconds between commands")
    p.add_argument("--soak-duration", type=float, default=120.0)
    p.add_argument("--jsonl", type=Path, default=None,
                   help="write per-step JSONL log to this path")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--log", default="INFO")
    args = p.parse_args()
    setup_logging(args.log)
    if args.seed is not None:
        random.seed(args.seed)

    jsonl = open(args.jsonl, "w") if args.jsonl else None
    records: list[StepRecord] = []
    reconnect_count = 0

    def disc_cb():
        _LOGGER.warning("DISCONNECT callback fired during run")

    controller = await connect(name=args.name, on_disconnect=disc_cb)
    if controller is None:
        _LOGGER.error("Initial connect failed; aborting")
        return 2

    # Build sequence
    if args.mode == "random":
        weights = DEFAULT_WEIGHTS
        cmd_pool = list(weights.keys())
        cmd_w = list(weights.values())
        sequence = random.choices(cmd_pool, weights=cmd_w, k=args.iterations)
    elif args.mode == "scenario":
        scenario = SCENARIOS[args.scenario]
        sequence = scenario * args.iterations
    elif args.mode == "soak":
        # Mostly update_state and short bursts
        sequence = []
        sequence.append("start")
        elapsed = 0.0
        while elapsed < args.soak_duration:
            sequence.append("update_state")
            elapsed += args.inter_cmd_gap
        sequence.append("stop")
    else:
        return 1

    _LOGGER.info("Running %d commands in mode=%s", len(sequence), args.mode)

    for i, cmd in enumerate(sequence):
        if not controller.connected:
            _LOGGER.warning("Not connected before step %d; reconnecting", i)
            with contextlib.suppress(Exception):
                await safe_disconnect(controller)
            controller = await connect(name=args.name, on_disconnect=disc_cb)
            reconnect_count += 1
            if controller is None:
                _LOGGER.error("Reconnect failed; aborting at step %d", i)
                break

        cmd_args_factory = COMMANDS[cmd][1]
        cmd_args = cmd_args_factory()
        rec, _ = await _run_step(controller, cmd, cmd_args, i)
        records.append(rec)
        _LOGGER.info(
            "%3d/%3d %s ok=%-5s lat=%5.0fms speed %s->%s err=%s",
            i + 1, len(sequence), cmd, rec.ok, rec.latency_ms,
            rec.before_speed, rec.after_speed, rec.error,
        )
        if jsonl:
            jsonl.write(json.dumps(rec.__dict__, default=str) + "\n")
            jsonl.flush()

        await asyncio.sleep(args.inter_cmd_gap)

    # Cleanup
    if controller and controller.connected:
        with contextlib.suppress(Exception):
            await controller.stop()
        await safe_disconnect(controller)

    if jsonl:
        jsonl.close()

    invariant_issues: list[str] = []
    if args.mode == "scenario" and args.scenario in SCENARIO_CHECKS:
        invariant_issues = SCENARIO_CHECKS[args.scenario](records)

    _summarise(records, reconnect_count, invariant_issues)
    return 0 if not invariant_issues else 1


def _summarise(
    records: list[StepRecord],
    reconnect_count: int,
    invariant_issues: list[str] | None = None,
) -> None:
    if not records:
        print("no records")
        return

    print()
    print("=" * 70)
    print("PER-COMMAND OUTCOMES")
    print("=" * 70)
    by_cmd: dict[str, list[StepRecord]] = defaultdict(list)
    for r in records:
        by_cmd[r.cmd].append(r)
    for cmd in sorted(by_cmd):
        rs = by_cmd[cmd]
        ok = sum(1 for r in rs if r.ok)
        lat = sum(r.latency_ms for r in rs) / len(rs)
        print(f"  {cmd:18s}  {ok:3d}/{len(rs):3d} ok   avg lat {lat:6.0f}ms")

    print()
    print("=" * 70)
    print("ERRORS")
    print("=" * 70)
    errs = Counter()
    for r in records:
        if r.error:
            errs[(r.cmd, r.error[:80])] += 1
    if not errs:
        print("  (none)")
    else:
        for (cmd, err), n in errs.most_common():
            print(f"  {n:3d}× {cmd:18s} {err}")

    print()
    print("=" * 70)
    print("TRANSITION FAILURES (cmd_prev -> cmd_curr where curr failed)")
    print("=" * 70)
    transitions = Counter()
    for prev, curr in zip(records, records[1:]):
        if not curr.ok:
            transitions[(prev.cmd, curr.cmd)] += 1
    for (a, b), n in transitions.most_common(15):
        print(f"  {n:3d}× {a:18s} -> {b}")
    if not transitions:
        print("  (no failures)")

    print()
    print("=" * 70)
    print("SCENARIO INVARIANTS")
    print("=" * 70)
    if invariant_issues is None:
        print("  (no scenario-level checks for this run)")
    elif not invariant_issues:
        print("  (all invariants held)")
    else:
        for issue in invariant_issues:
            print(f"  - {issue}")

    print()
    print("=" * 70)
    print("RUN STATS")
    print("=" * 70)
    total = len(records)
    total_ok = sum(1 for r in records if r.ok)
    print(f"  total commands : {total}")
    print(f"  total ok       : {total_ok} ({total_ok / total * 100:.1f}%)")
    print(f"  reconnects     : {reconnect_count}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
