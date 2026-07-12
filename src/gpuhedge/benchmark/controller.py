"""Stage 2 controller — the 48-round three-provider MOSS cold-start dataset.

Eight blocks of six rounds (benchmarks/2026-07-moss/methodology.md §Stage 2). Between rounds the
controller waits past every provider's idle/scale-down window to force a genuine
cold start (it does NOT redeploy — a redeploy invalidates RunPod's FlashBoot
cache and would test deployment churn instead of normal scale-to-zero). The
projected-cost ledger enforces the $29 "moss_trace_complete" gate; the operator
is prompted to reconcile against provider dashboards after every block.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from gpuhedge.backends import Backend, build_backend
from gpuhedge.benchmark.round import run_paired_round
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import (
    BudgetExceeded,
    CostLedger,
    CostMonitor,
    TraceWriter,
    format_snapshot,
)

# Wait long enough to clear the slowest idle window (RunPod idle_timeout=60 s;
# the gpuhedge Modal/Cerebrium deploys use short scale-down so this dominates).
DEFAULT_INTER_ROUND_WAIT_S = 130.0

Logger = Callable[[str], None]


def build_backends(
    config: BenchmarkConfig, provider_keys: list[str]
) -> list[Backend]:
    return [build_backend(config.provider(k), config.request) for k in provider_keys]


def missing_deployments(backends: list[Backend]) -> list[str]:
    return [b.key for b in backends if not b.available()]


async def force_cold(
    backends: list[Backend], *, wait_s: float, log: Logger, sleep=asyncio.sleep
) -> None:
    """Best-effort return every endpoint to its cold state before a round."""

    for b in backends:
        try:
            await b.scale_to_zero()
        except Exception as exc:  # noqa: BLE001 - modal's is intentionally manual
            log(f"  [{b.key}] scale_to_zero note: {str(exc)[:120]}")
    log(f"  waiting {wait_s:.0f}s for scale-to-zero (forces a real cold start)...")
    await sleep(wait_s)


async def run_moss_stage(
    config: BenchmarkConfig,
    *,
    log: Logger = print,
    inter_round_wait_s: float = DEFAULT_INTER_ROUND_WAIT_S,
    start_round: int = 1,
    max_rounds: int | None = None,
    warm_companion: bool | None = None,
    dry_run: bool = False,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    """Run the MOSS cold-start dataset. Resumable via ``start_round``.

    ``dry_run`` builds backends and prints the plan without submitting jobs — the
    safe way to inspect the harness before spending money."""

    stage_cfg = config.stages["moss"]
    provider_keys = stage_cfg["providers"]
    blocks = int(stage_cfg["blocks"])
    rounds_per_block = int(stage_cfg["rounds_per_block"])
    total_rounds = blocks * rounds_per_block
    if max_rounds is not None:
        total_rounds = min(total_rounds, start_round - 1 + max_rounds)
    warm = stage_cfg.get("warm_companion", True) if warm_companion is None else warm_companion

    backends = build_backends(config, provider_keys)
    absent = missing_deployments(backends)

    log(f"Stage 2 — MOSS cold-start dataset: {blocks} blocks x {rounds_per_block} "
        f"rounds = {blocks * rounds_per_block} paired rounds")
    log(f"  providers: {provider_keys}")
    log(f"  per-provider cap: {config.moss_timeout_s():.0f}s (unfinished => right-censored)")
    log(f"  warm companion: {warm}")
    log(f"  gate: moss_trace_complete <= ${config.budget.gates['moss_trace_complete']:.0f}")
    if absent:
        log(f"  NOT DEPLOYED: {absent} — deploy these first (see deploy/). "
            "Rounds will record SUBMIT_FAILED for them.")

    if dry_run:
        log("  dry-run: not submitting any jobs.")
        return {"dry_run": True, "planned_rounds": total_rounds, "not_deployed": absent}

    ledger = CostLedger(config)
    trace = TraceWriter(config.trace_dir() / "moss_rounds.jsonl")
    monitor = CostMonitor(config)
    completed = 0
    try:
        snap = monitor.snapshot("bench-start", projected_total=ledger.projected_total)
        log(f"  costs @ start: {format_snapshot(snap)}")
        for rid in range(start_round, total_rounds + 1):
            block = (rid - 1) // rounds_per_block + 1
            log(f"[round {rid}/{total_rounds}] block {block} — "
                f"projected ${ledger.projected_total:.2f}")
            try:
                result = await run_paired_round(
                    config, backends, ledger, trace,
                    round_id=rid, block=block, stage="moss", warm_companion=warm,
                )
            except BudgetExceeded as exc:
                log(f"  BUDGET STOP: {exc}")
                break
            winner = result.winner or "none-valid"
            log(f"  winner={winner} @ {result.winner_wall_s}s | "
                + " ".join(f"{k}={o.state}/{o.wall_s:.0f}s" for k, o in result.outcomes.items()))
            completed += 1

            # Actual-vs-projected cost snapshot + reconciliation at block ends.
            if rid % rounds_per_block == 0:
                snap = monitor.snapshot(
                    f"block-{block}-end", projected_total=ledger.projected_total
                )
                log(f"  -- block {block} costs: {format_snapshot(snap)} --")
                for provider, spent in snap["actual_spend_since_baseline"].items():
                    ledger.reconcile(provider, spent, note=f"auto block-{block}")
            if rid < total_rounds:
                await force_cold(backends, wait_s=inter_round_wait_s, log=log, sleep=sleep)
    finally:
        snap = monitor.snapshot("bench-end", projected_total=ledger.projected_total)
        log(f"  costs @ end: {format_snapshot(snap)}")
        monitor.close()
        trace.close()
        ledger.close()

    summary = ledger.summary()
    summary.update({"rounds_completed": completed, "not_deployed": absent})
    log(f"Stage 2 done: {completed} rounds, projected ${summary['projected_total_usd']:.2f}")
    return summary
