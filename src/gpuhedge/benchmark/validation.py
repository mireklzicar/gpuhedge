"""Pre-registered randomized validation driver (docs/policies.md).

Reads a preregistration YAML (see ``benchmarks/*/preregistration.yaml``) and
executes it verbatim: randomized single-arm blocks, per-block provider billing
snapshots, fixed cadence, one idle gap, right-censoring. The prereg file is
committed before the run; this driver takes every parameter from it so the
executed experiment IS the registered one.

Arms:
  single-runpod    submit primary, wait for the result, no hedging
  fixed-hedge-10s  live_hedge.run_hedged_request (the Stage 3 policy)
  queue-cutover    state_aware.run_state_aware_request
  cascade          cascade.run_cascade_request (cutover + escalation)

Request-level interleaving: set ``requests_per_block: 1`` in the prereg
design and the seeded shuffle randomizes single requests instead of blocks —
the latency-experiment design recommended after the 2026-07 validation
(billing attribution still wants pure blocks; run that separately).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from gpuhedge.backends import build_backend
from gpuhedge.benchmark.live_hedge import run_hedged_request
from gpuhedge.benchmark.state_aware import run_state_aware_request
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import (
    BudgetExceeded,
    CostLedger,
    CostMonitor,
    TraceWriter,
    format_snapshot,
)
from gpuhedge.validators import get_validator

Logger = Callable[[str], None]

STAGE = "validation"


def load_prereg(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())


def block_order(prereg: dict[str, Any]) -> list[str]:
    """The registered randomization: seeded shuffle of arm-name blocks."""

    design = prereg["design"]
    arms = [a["name"] for a in prereg["arms"]]
    blocks = [arm for arm in arms for _ in range(int(design["blocks_per_arm"]))]
    random.Random(int(design["block_order_seed"])).shuffle(blocks)
    return blocks


async def single_provider_request(
    config: BenchmarkConfig, ledger: CostLedger, trace: TraceWriter, *,
    provider_key: str, timeout_s: float, request_id: int, stage: str = STAGE,
) -> dict[str, Any]:
    backend = build_backend(config.provider(provider_key), config.request)
    handle = await backend.submit()
    result = await handle.result(timeout_s)
    valid = get_validator(config)(result).valid
    ledger.charge(backend.provider, result.wall_s, stage=stage, note="single")
    record = {
        "kind": "single_request",
        "stage": stage,
        "request_id": request_id,
        "policy": f"single:{provider_key}",
        "winner": provider_key if valid else None,
        "winner_valid_at_ms": round(result.wall_s * 1000, 1) if valid else None,
        "winner_total_ms": round(result.wall_s * 1000, 1) if valid else None,
        "state": result.state.value,
        "valid": valid,
        "winner_metrics": result.provider_metrics,
        "cancellation": None,
    }
    trace.write(record)
    if valid and result.audio is not None:
        record["_winner_audio"] = result.audio
        record["_winner_sample_rate"] = result.sample_rate
    return record


async def run_validation_stage(
    config: BenchmarkConfig,
    prereg_path: str | Path,
    *,
    log: Logger = print,
    start_block: int = 1,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    prereg = load_prereg(prereg_path)
    design = prereg["design"]
    policy = prereg["policy_under_test"]
    primary = policy["primary"]
    hedge = policy["hedge"]
    machine = policy["state_machine"]
    cutover_ms = int(machine["poll_primary_status_at_ms"])
    safety_ms = int(machine["safety_hedge_at_ms"])
    per_block = int(design["requests_per_block"])
    wait_s = float(design["inter_request_wait_s"])
    idle_after = int(design.get("idle_gap_after_block", 0))
    idle_gap_s = float(design.get("idle_gap_s", 0))
    cap = float(design.get("timeout_s", config.moss_timeout_s()))

    order = block_order(prereg)
    ledger = CostLedger(config)
    trace = TraceWriter(config.trace_dir() / "validation.jsonl")
    monitor = CostMonitor(config)
    completed = 0

    log(f"Validation ({prereg['experiment']}): {len(order)} blocks x {per_block} "
        f"requests, order={order}")
    trace.write({"kind": "validation_plan", "stage": STAGE, "order": order,
                 "prereg": str(prereg_path), "start_block": start_block})
    try:
        req_id = (start_block - 1) * per_block
        for b, arm in enumerate(order, start=1):
            if b < start_block:
                continue
            snap = monitor.snapshot(f"val-b{b:02d}-{arm}-start",
                                    projected_total=ledger.projected_total)
            log(f"[block {b}/{len(order)}] arm={arm}  costs: {format_snapshot(snap)}")
            for i in range(per_block):
                req_id += 1
                try:
                    if arm == "single-runpod":
                        record = await single_provider_request(
                            config, ledger, trace, provider_key=primary,
                            timeout_s=cap, request_id=req_id)
                    elif arm == "fixed-hedge-10s":
                        record = await run_hedged_request(
                            config, ledger, trace, primary_key=primary,
                            hedge_key=hedge, hedge_after_ms=10_000,
                            timeout_s=cap, request_id=req_id)
                    elif arm == "cascade":
                        from gpuhedge.benchmark.cascade import run_cascade_request

                        record = await run_cascade_request(
                            config, ledger, trace, primary_key=primary,
                            hedge_key=hedge,
                            fallback_key=policy["fallback"],
                            queue_cutover_ms=cutover_ms,
                            safety_hedge_ms=safety_ms,
                            escalate_after_ms=int(
                                machine.get("escalate_at_ms", 25_000)),
                            timeout_s=cap, request_id=req_id, stage=STAGE)
                    else:  # queue-cutover
                        record = await run_state_aware_request(
                            config, ledger, trace, primary_key=primary,
                            hedge_key=hedge, queue_cutover_ms=cutover_ms,
                            safety_hedge_ms=safety_ms, timeout_s=cap,
                            request_id=req_id, stage=STAGE)
                except BudgetExceeded as exc:
                    log(f"  BUDGET STOP: {exc}")
                    raise
                # annotate the arm/block onto the already-written record via a
                # small index entry (records themselves stay adapter-shaped)
                trace.write({"kind": "validation_index", "stage": STAGE,
                             "request_id": req_id, "block": b, "arm": arm,
                             "policy": record.get("policy"),
                             "winner": record.get("winner"),
                             "winner_valid_at_ms": record.get("winner_valid_at_ms"),
                             "winner_total_ms": record.get("winner_total_ms"),
                             "cutover_fired": record.get("cutover_fired"),
                             "safety_hedge_fired": record.get("safety_hedge_fired"),
                             "escalation_fired": record.get("escalation_fired"),
                             "cancelled": bool(record.get("cancellation"))})
                completed += 1
                log(f"  [req {req_id}] {arm}: winner={record.get('winner')} "
                    f"total={record.get('winner_total_ms')}ms"
                    + (f" cutover={record.get('cutover_fired')}"
                       if arm == "queue-cutover" else ""))
                is_last_of_run = b == len(order) and i == per_block - 1
                if not is_last_of_run:
                    await sleep(wait_s)
            snap = monitor.snapshot(f"val-b{b:02d}-{arm}-end",
                                    projected_total=ledger.projected_total)
            log(f"[block {b}] done  costs: {format_snapshot(snap)}")
            if idle_after and b == idle_after and b < len(order):
                log(f"  idle gap: {idle_gap_s:.0f}s")
                await sleep(idle_gap_s)
    except BudgetExceeded:
        pass
    finally:
        snap = monitor.snapshot("validation-end",
                                projected_total=ledger.projected_total)
        log(f"costs @ end: {format_snapshot(snap)}")
        monitor.close()
        trace.close()
        ledger.close()

    return {"requests_completed": completed,
            "projected_total_usd": ledger.projected_total}
