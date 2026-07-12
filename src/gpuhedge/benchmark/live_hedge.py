"""Stage 3 — live hedged request: the actual GPUHedge policy end to end.

Start the primary; if it has not returned a *valid* result after ``hedge_after_ms``,
launch one hedge; return the first valid result; remotely cancel the loser and
capture a cancellation receipt (was it running, ack latency, cancel->terminal,
estimated wasted GPU-$). At most two active jobs, ever.

The complete Stage 2 traces already let hundreds of policies be simulated
offline; Stage 3 exists only to verify that the simulated savings survive real
cancellation behaviour (benchmarks/2026-07-moss/methodology.md, Stage 3).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from gpuhedge.backends import JobHandle, ProviderResult, build_backend
from gpuhedge.backends.base import now_ms
from gpuhedge.benchmark.state_aware import safe_cancel
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import CostLedger, TraceWriter
from gpuhedge.validators import Validator, get_validator


async def _resolve(handle: JobHandle, timeout_s: float) -> tuple[JobHandle, ProviderResult]:
    return handle, await handle.result(timeout_s)


async def run_hedged_request(
    config: BenchmarkConfig,
    ledger: CostLedger,
    trace: TraceWriter,
    *,
    primary_key: str,
    hedge_key: str,
    hedge_after_ms: int | None = None,
    timeout_s: float | None = None,
    request_id: int = 0,
) -> dict[str, Any]:
    """Run one primary->hedge race with real loser cancellation."""

    cap = timeout_s if timeout_s is not None else config.moss_timeout_s()
    delay_s = (
        hedge_after_ms if hedge_after_ms is not None else config.policy["hedge_after_ms"]
    ) / 1000.0
    validator = get_validator(config)

    primary = build_backend(config.provider(primary_key), config.request)
    hedge = build_backend(config.provider(hedge_key), config.request)

    # Submit primary first so we hold its handle for a possible cancel.
    t0 = now_ms()
    primary_handle = await primary.submit()
    handles: dict[str, JobHandle] = {primary_key: primary_handle}
    tasks: dict[asyncio.Task, str] = {
        asyncio.create_task(_resolve(primary_handle, cap)): primary_key
    }

    launched_hedge = False
    winner_key: str | None = None
    winner_result: ProviderResult | None = None

    # Phase 1: give the primary its fast-path window before launching the hedge.
    pending = set(tasks)
    done, pending = await asyncio.wait(
        pending, timeout=delay_s, return_when=asyncio.FIRST_COMPLETED
    )
    winner_key, winner_result = _first_valid(done, tasks, validator)

    if winner_result is None:
        # Primary hasn't produced a valid result within the delay -> hedge.
        launched_hedge = True
        hedge_handle = await hedge.submit()
        handles[hedge_key] = hedge_handle
        htask = asyncio.create_task(_resolve(hedge_handle, cap))
        tasks[htask] = hedge_key
        pending.add(htask)

        # Phase 2: first VALID result wins (a fast invalid result keeps waiting).
        while pending and winner_result is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            winner_key, winner_result = _first_valid(done, tasks, validator)

    # Cancel the loser if it is still in flight, and settle its task.
    receipt = None
    if winner_key is not None:
        loser_key = hedge_key if winner_key == primary_key else primary_key
        loser_handle = handles.get(loser_key)
        if loser_handle is not None and _still_running(loser_key, tasks):
            # Poll once so the receipt's was_running reflects the actual
            # lifecycle phase at cancel time (not a stale submit-time default).
            try:
                await loser_handle.status()
            except Exception:  # noqa: BLE001 - best-effort
                pass
            receipt = await safe_cancel(loser_handle, reason="lost the race")
    for task in tasks:
        if not task.done():
            task.cancel()

    # Projected cost: winner wall + any loser GPU-seconds burned before cancel.
    if winner_result is not None and winner_key is not None:
        ledger.charge(config.provider(winner_key), winner_result.wall_s,
                      stage="live_hedge", note="winner")
    if receipt is not None and receipt.estimated_cost_usd:
        ledger.charge_usd(receipt.provider, receipt.estimated_cost_usd,
                          stage="live_hedge", note="loser before cancel")

    # End-to-end latency from request start: the winner's wall time is relative
    # to ITS OWN submit, so a hedge win adds the hedge's launch offset.
    winner_total_ms = None
    if winner_result is not None and winner_key is not None:
        winner_handle = handles[winner_key]
        winner_total_ms = round(
            (winner_handle.submit_ms - t0) + winner_result.wall_s * 1000, 1
        )

    record = {
        "kind": "hedged_request",
        "stage": "live_hedge",
        "request_id": request_id,
        "policy": f"{primary_key}->{hedge_key}@{delay_s:.0f}s",
        "hedge_launched": launched_hedge,
        "winner": winner_key,
        "winner_valid_at_ms": round(winner_result.wall_s * 1000, 1) if winner_result else None,
        "winner_total_ms": winner_total_ms,
        "cancellation": asdict(receipt) if receipt else None,
    }
    trace.write(record)
    return record


def _first_valid(
    done: set[asyncio.Task], tasks: dict[asyncio.Task, str],
    validator: Validator,
) -> tuple[str | None, ProviderResult | None]:
    """Return the (provider, result) of the first finished task that validates."""

    for task in done:
        if task.cancelled() or task.exception() is not None:
            continue
        handle, result = task.result()
        if validator(result).valid:
            return handle.provider, result
    return None, None


def _still_running(provider_key: str, tasks: dict[asyncio.Task, str]) -> bool:
    for task, key in tasks.items():
        if key == provider_key:
            return not task.done()
    return False
