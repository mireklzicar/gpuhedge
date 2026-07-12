"""Stage 3 controller — 18 live hedged requests (benchmarks/2026-07-moss/methodology.md, Stage 3).

Policies from config ``stages.live_hedging``:
  - runpod->modal @ 10 s        (6 requests)
  - runpod->cerebrium @ 10 s    (6 requests)
  - adaptive two-of-three       (6 requests): primary runpod; the hedge is
    whichever of modal/cerebrium currently has the better observed deadline
    probability per incremental dollar, estimated from the Stage 2 traces
    available *before* each request (no future information).

Each request produces the full race record + a cancellation receipt for the
loser. Between requests the controller waits past every scale-down window so
races start from the normal cold state. Gate: live_policy_complete <= $37.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

from gpuhedge.benchmark.controller import build_backends, force_cold
from gpuhedge.benchmark.live_hedge import run_hedged_request
from gpuhedge.benchmark.replay import load_rounds
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import (
    BudgetExceeded,
    CostLedger,
    CostMonitor,
    TraceWriter,
    format_snapshot,
)

Logger = Callable[[str], None]

INF = math.inf


def _adaptive_hedge_choice(
    config: BenchmarkConfig, candidates: list[str], *, deadline_s: float,
    hedge_after_s: float = 0.0,
) -> tuple[str, dict[str, Any]]:
    """Pick the hedge with the best P(finish <= remaining deadline) per $ from
    traces recorded so far (uses only already-observed rounds).

    The hedge launches ``hedge_after_s`` into the request, so it must finish
    within the REMAINING budget (deadline - hedge_after_s), not the original
    deadline."""

    remaining_s = max(1.0, deadline_s - hedge_after_s)
    rounds = load_rounds(config.trace_dir() / "moss_rounds.jsonl")
    scores: dict[str, dict[str, float]] = {}
    best_key, best_score = candidates[0], -INF
    for key in candidates:
        lats = [r.latency.get(key, INF) for r in rounds if key in r.latency]
        recent = lats[-12:]  # most recent window
        if recent:
            p_hit = sum(1 for x in recent if x <= remaining_s) / len(recent)
            finite = [x for x in recent if not math.isinf(x)]
            mean_s = sum(finite) / len(finite) if finite else remaining_s
        else:
            p_hit, mean_s = 0.0, remaining_s
        cost = mean_s * config.provider(key).billed_rate_per_s
        score = p_hit / cost if cost > 0 else 0.0
        scores[key] = {"p_hit": round(p_hit, 3), "mean_s": round(mean_s, 1),
                       "remaining_s": round(remaining_s, 1),
                       "est_cost": round(cost, 5), "score": round(score, 2)}
        if score > best_score:
            best_key, best_score = key, score
    return best_key, scores


async def run_live_hedging_stage(
    config: BenchmarkConfig,
    *,
    log: Logger = print,
    inter_request_wait_s: float = 130.0,
    start_request: int = 1,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    stage_cfg = config.stages["live_hedging"]
    hedge_after_ms = int(config.policy.get("hedge_after_ms", 10000))
    deadline_s = float(config.slo.get("primary_deadline_s", 60))

    # Expand the policy plan into an ordered request list.
    plan: list[dict[str, Any]] = []
    for pol in stage_cfg["policies"]:
        for _ in range(int(pol["n"])):
            plan.append({"name": pol["name"]})

    ledger = CostLedger(config)
    trace = TraceWriter(config.trace_dir() / "live_hedge.jsonl")
    monitor = CostMonitor(config)
    completed = 0

    log(f"Stage 3 — live hedging: {len(plan)} requests, hedge@{hedge_after_ms}ms, "
        f"gate live_policy_complete <= ${config.budget.gates['live_policy_complete']:.0f}")
    try:
        snap = monitor.snapshot("stage3-start", projected_total=ledger.projected_total)
        log(f"  costs @ start: {format_snapshot(snap)}")
        for i, item in enumerate(plan[start_request - 1:], start=start_request):
            name = item["name"]
            if name == "runpod->modal@10s":
                primary, hedge = "runpod", "modal"
                choice_info = None
            elif name == "runpod->cerebrium@10s":
                primary, hedge = "runpod", "cerebrium"
                choice_info = None
            else:  # adaptive-two-of-three
                primary = config.policy.get("primary", "runpod")
                hedge, choice_info = _adaptive_hedge_choice(
                    config, list(config.policy.get("hedge_choose_one_of",
                                                   ["modal", "cerebrium"])),
                    deadline_s=deadline_s, hedge_after_s=hedge_after_ms / 1000.0,
                )
            log(f"[req {i}/{len(plan)}] {name} -> primary={primary} hedge={hedge}"
                + (f" (adaptive: {choice_info})" if choice_info else ""))
            try:
                record = await run_hedged_request(
                    config, ledger, trace,
                    primary_key=primary, hedge_key=hedge,
                    hedge_after_ms=hedge_after_ms, request_id=i,
                )
            except BudgetExceeded as exc:
                log(f"  BUDGET STOP: {exc}")
                break
            record["policy_name"] = name
            if choice_info:
                record["adaptive_scores"] = choice_info
            winner = record.get("winner")
            cancel = record.get("cancellation") or {}
            log(f"  winner={winner} @ {record.get('winner_valid_at_ms')}ms | "
                f"hedged={record.get('hedge_launched')} | "
                f"loser_cancel: ack={cancel.get('cancel_ack_ms') is not None} "
                f"leaked={cancel.get('leaked')}")
            completed += 1
            if i < len(plan):
                backends = build_backends(config, [primary, hedge])
                await force_cold(backends, wait_s=inter_request_wait_s, log=log, sleep=sleep)
    finally:
        snap = monitor.snapshot("stage3-end", projected_total=ledger.projected_total)
        log(f"  costs @ end: {format_snapshot(snap)}")
        monitor.close()
        trace.close()
        ledger.close()

    return {"requests_completed": completed, "projected_total_usd": ledger.projected_total}
