"""Cascaded state-aware hedge — cutover, safety hedge, then a second-level
fallback for the hedge provider's own tail (docs/policies.md).

    t = 0         submit primary
    t = cutover   poll primary state:
                    still IN_QUEUE -> cancel primary, launch hedge
                    running        -> keep it
    t = safety    primary kept but no valid result -> launch hedge
    t = escalate  no valid result AND fewer than two attempts still live ->
                  launch the fallback provider
    always        first VALID result wins; every loser is cancelled with an
                  audited receipt; never more than two live GPU jobs

Motivation (benchmarks/2026-07-queue-cutover/results.md): the queue-cutover
validation caught one request that correctly abandoned the primary and then
spent 104.2 s on the hedge provider's own tail. The primary can have a tail,
but so can the hedge — so the cascade escalates to a third provider, paying
for it only when the earlier stages are already unhealthy.

Concurrency rule: an attempt counts as live until its result task finishes or
its remote cancellation is confirmed terminal. Escalation is skipped (and
recorded) while two attempts are live; it is re-checked whenever one resolves.
As in state_aware, a cancelled-but-unconfirmed attempt stays in the race — a
leaked primary can still rescue a failing hedge.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from gpuhedge.backends import (
    CancellationReceipt,
    JobHandle,
    JobState,
    ProviderResult,
    build_backend,
)
from gpuhedge.backends.base import now_ms
from gpuhedge.benchmark.state_aware import safe_cancel
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import CostLedger, TraceWriter
from gpuhedge.validators import Validator, get_validator


async def _resolve(handle: JobHandle, timeout_s: float) -> tuple[JobHandle, ProviderResult]:
    return handle, await handle.result(timeout_s)


class _Attempt:
    def __init__(self, key: str, handle: JobHandle, task: asyncio.Task) -> None:
        self.key = key
        self.handle = handle
        self.task = task
        self.cancel_task: asyncio.Task | None = None
        self.receipt: CancellationReceipt | None = None

    @property
    def live(self) -> bool:
        """Counts against the two-job cap: not finished locally and not
        confirmed stopped remotely."""

        if self.task.done():
            return False
        if self.receipt is not None and self.receipt.confirmed_terminal:
            return False
        return True


async def run_cascade_request(
    config: BenchmarkConfig,
    ledger: CostLedger,
    trace: TraceWriter,
    *,
    primary_key: str,
    hedge_key: str,
    fallback_key: str,
    queue_cutover_ms: int = 2500,
    safety_hedge_ms: int = 8500,
    escalate_after_ms: int = 25000,
    max_live_jobs: int = 2,
    timeout_s: float | None = None,
    request_id: int = 0,
    stage: str = "cascade",
) -> dict[str, Any]:
    """One cascaded request; returns the full trace record."""

    cap = timeout_s if timeout_s is not None else config.moss_timeout_s()
    cutover_s = queue_cutover_ms / 1000.0
    safety_s = safety_hedge_ms / 1000.0
    escalate_s = escalate_after_ms / 1000.0
    validator = get_validator(config)

    def _valid(result: ProviderResult | None,
               _v: Validator = validator) -> bool:
        return result is not None and _v(result).valid

    backends = {
        key: build_backend(config.provider(key), config.request)
        for key in (primary_key, hedge_key, fallback_key)
    }

    t0 = now_ms()

    def _elapsed_s() -> float:
        return (now_ms() - t0) / 1000.0

    attempts: dict[str, _Attempt] = {}

    async def _submit(key: str) -> _Attempt:
        handle = await backends[key].submit()
        attempt = _Attempt(key, handle, asyncio.create_task(_resolve(handle, cap)))
        attempts[key] = attempt
        record["submits"].append({"provider": key,
                                  "at_ms": round(now_ms() - t0, 1)})
        return attempt

    record: dict[str, Any] = {
        "kind": "cascade_request",
        "stage": stage,
        "request_id": request_id,
        "policy": f"cascade:{primary_key}->{hedge_key}->{fallback_key}"
                  f"@q{cutover_s:g}s+s{safety_s:g}s+e{escalate_s:g}s",
        "cutover_fired": False,
        "safety_hedge_fired": False,
        "escalation_fired": False,
        "escalation_skipped_at_capacity": False,
        "submits": [],
    }

    await _submit(primary_key)
    primary = attempts[primary_key]

    winner_key: str | None = None
    winner_result: ProviderResult | None = None

    def _collect(done: set[asyncio.Task]) -> None:
        nonlocal winner_key, winner_result
        if winner_result is not None:
            return
        for task in done:
            if task.cancelled() or task.exception() is not None:
                continue
            handle, result = task.result()
            if _valid(result):
                winner_key, winner_result = handle.provider, result
                return

    # ---- t = cutover: poll the primary's lifecycle state -------------------
    done, _ = await asyncio.wait({primary.task}, timeout=cutover_s)
    _collect(done)

    if winner_result is None and not primary.task.done():
        poll_start = now_ms()
        try:
            state_at_poll = await primary.handle.status()
        except Exception as exc:  # noqa: BLE001 - poll failure -> keep primary
            state_at_poll = JobState.UNKNOWN
            record["poll_error"] = str(exc)[:200]
        record["state_at_poll"] = state_at_poll.value
        record["poll_at_ms"] = round(poll_start - t0, 1)

        if state_at_poll is JobState.QUEUED:
            # cutover: abandon the unstarted primary, switch to the hedge
            record["cutover_fired"] = True
            primary.cancel_task = asyncio.create_task(
                safe_cancel(primary.handle, reason="still queued at cutover poll")
            )
            await _submit(hedge_key)

    # ---- t = safety: primary kept but still no valid result ----------------
    if winner_result is None and hedge_key not in attempts:
        done, _ = await asyncio.wait(
            {primary.task}, timeout=max(0.0, safety_s - _elapsed_s())
        )
        _collect(done)
        if winner_result is None:
            # primary is still running, or finished invalid/failed: either
            # way the hedge is now needed.
            record["safety_hedge_fired"] = True
            await _submit(hedge_key)

    # ---- main race with the escalation deadline -----------------------------
    while winner_result is None:
        # settle any finished cancel tasks so `live` reflects confirmations
        for attempt in attempts.values():
            if attempt.cancel_task is not None and attempt.cancel_task.done():
                attempt.receipt = attempt.cancel_task.result()
                attempt.cancel_task = None

        pending = {a.task for a in attempts.values() if not a.task.done()}
        may_escalate = fallback_key not in attempts

        # Escalate at the deadline — or immediately if every earlier attempt
        # already resolved without a valid result.
        if may_escalate and (not pending or _elapsed_s() >= escalate_s):
            live = sum(1 for a in attempts.values() if a.live)
            if live < max_live_jobs:
                record["escalation_fired"] = True
                await _submit(fallback_key)
                continue
            # two attempts genuinely live: honour the cap, re-check when one
            # of them resolves
            record["escalation_skipped_at_capacity"] = True

        if not pending:
            break  # every attempt resolved without a valid result

        timeout = None
        if may_escalate and _elapsed_s() < escalate_s:
            timeout = escalate_s - _elapsed_s()
        done, _ = await asyncio.wait(
            pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        _collect(done)

    # ---- cancel every in-flight loser ---------------------------------------
    receipts: list[CancellationReceipt] = []
    for attempt in attempts.values():
        if attempt.cancel_task is not None:
            attempt.receipt = await attempt.cancel_task
            attempt.cancel_task = None
        if attempt.receipt is None and attempt.key != winner_key \
                and not attempt.task.done():
            try:
                await attempt.handle.status()
            except Exception:  # noqa: BLE001 - best-effort
                pass
            attempt.receipt = await safe_cancel(attempt.handle,
                                                reason="lost the race")
        if attempt.receipt is not None:
            receipts.append(attempt.receipt)
        if not attempt.task.done():
            attempt.task.cancel()

    if winner_result is not None and winner_key is not None:
        ledger.charge(config.provider(winner_key), winner_result.wall_s,
                      stage=stage, note="winner")
    for receipt in receipts:
        if receipt.estimated_cost_usd:
            ledger.charge_usd(receipt.provider, receipt.estimated_cost_usd,
                              stage=stage, note="loser before cancel")

    winner_total_ms = None
    if winner_result is not None and winner_key is not None:
        winner_handle = attempts[winner_key].handle
        winner_total_ms = round(
            (winner_handle.submit_ms - t0) + winner_result.wall_s * 1000, 1
        )

    record.update({
        "winner": winner_key,
        "winner_valid_at_ms": (
            round(winner_result.wall_s * 1000, 1) if winner_result else None
        ),
        "winner_total_ms": winner_total_ms,
        "winner_metrics": winner_result.provider_metrics if winner_result else None,
        "cancellation": asdict(receipts[0]) if receipts else None,
        "cancellations": [asdict(r) for r in receipts] if len(receipts) > 1 else None,
    })
    trace.write(record)
    # Attach the winning payload AFTER the trace write so the JSONL stays
    # bytes-free; the Router surfaces these as RouterOutcome.audio.
    if winner_result is not None and winner_result.audio is not None:
        record["_winner_audio"] = winner_result.audio
        record["_winner_sample_rate"] = winner_result.sample_rate
    return record
