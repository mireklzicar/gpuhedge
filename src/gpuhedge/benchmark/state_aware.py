"""Queue-state-aware cutover — the live policy (docs/policies.md).

    t = 0        submit primary
    t = cutover  poll primary job state
                   still IN_QUEUE  -> cancel primary BEFORE its worker starts,
                                      launch the hedge (full switch)
                   IN_PROGRESS     -> keep the primary (likely fast path)
    t = safety   primary running but no valid result yet -> launch the hedge
                 as a fallback; first VALID result wins; cancel the loser

The point vs the fixed timer hedge: the primary's own lifecycle state predicts
its slow mode by ~2.5 s (RunPod queue delays: fast path 1.1-2.0 s, fresh
worker 8.9-27.6 s, no overlap in 54 rounds), so the policy can abandon a cold
start before paying for most of it — and, if cancelling a queued job is
unbilled, before paying for ANY of it. That billing assumption is exactly
what the pre-registered validation measures.

Failure handling: a cutover keeps the primary's result task in the race until
remote cancellation is confirmed — if the cancel fails or leaks and the hedge
then fails or returns invalid output, the primary's own result can still win
instead of the router returning nothing while the remote job burns on.
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
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import CostLedger, TraceWriter
from gpuhedge.validators import Validator, get_validator


async def _resolve(handle: JobHandle, timeout_s: float) -> tuple[JobHandle, ProviderResult]:
    return handle, await handle.result(timeout_s)


async def safe_cancel(handle: JobHandle, *, reason: str) -> CancellationReceipt:
    """Cancel that can never take the request down: an adapter exception
    becomes a NO_EVIDENCE / leaked receipt instead of propagating."""

    try:
        return await handle.cancel(reason=reason)
    except Exception as exc:  # noqa: BLE001 - loser cleanup must not kill the race
        return CancellationReceipt(
            provider=handle.provider, job_id=handle.job_id(),
            was_running=False, cancel_sent_ms=now_ms(),
            note=f"cancel raised: {exc}"[:200],
        )


def _first_valid(
    done: set[asyncio.Task], validator: Validator,
) -> tuple[str | None, ProviderResult | None]:
    for task in done:
        if task.cancelled() or task.exception() is not None:
            continue
        handle, result = task.result()
        if validator(result).valid:
            return handle.provider, result
    return None, None


async def run_state_aware_request(
    config: BenchmarkConfig,
    ledger: CostLedger,
    trace: TraceWriter,
    *,
    primary_key: str,
    hedge_key: str,
    queue_cutover_ms: int = 2500,
    safety_hedge_ms: int = 8500,
    timeout_s: float | None = None,
    request_id: int = 0,
    stage: str = "state_aware",
) -> dict[str, Any]:
    """One live state-aware request; returns the full trace record."""

    cap = timeout_s if timeout_s is not None else config.moss_timeout_s()
    cutover_s = queue_cutover_ms / 1000.0
    safety_s = safety_hedge_ms / 1000.0
    validator = get_validator(config)

    def _valid(result: ProviderResult | None,
               _v: Validator = validator) -> bool:
        return result is not None and _v(result).valid

    primary = build_backend(config.provider(primary_key), config.request)
    hedge = build_backend(config.provider(hedge_key), config.request)

    t0 = now_ms()
    primary_handle = await primary.submit()
    primary_task = asyncio.create_task(_resolve(primary_handle, cap))

    record: dict[str, Any] = {
        "kind": "state_aware_request",
        "stage": stage,
        "request_id": request_id,
        "policy": f"cutover:{primary_key}->{hedge_key}"
                  f"@q{cutover_s:g}s+s{safety_s:g}s",
        "cutover_fired": False,
        "safety_hedge_fired": False,
    }

    # ---- t = cutover: poll the primary's lifecycle state -------------------
    done, _ = await asyncio.wait({primary_task}, timeout=cutover_s)
    winner_key: str | None = None
    winner_result: ProviderResult | None = None
    if done:  # finished before the poll (not observed on cold starts)
        _, result = primary_task.result()
        if _valid(result):
            winner_key, winner_result = primary_key, result

    receipts: list[CancellationReceipt] = []
    hedge_handle: JobHandle | None = None
    hedge_task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None

    if winner_result is None:
        poll_start = now_ms()
        try:
            state_at_poll = await primary_handle.status()
        except Exception as exc:  # noqa: BLE001 - poll failure -> keep primary
            state_at_poll = JobState.UNKNOWN
            record["poll_error"] = str(exc)[:200]
        record["state_at_poll"] = state_at_poll.value
        record["poll_at_ms"] = round(poll_start - t0, 1)
        record["poll_latency_ms"] = round(now_ms() - poll_start, 1)

        if state_at_poll is JobState.QUEUED:
            # -------- cutover: cancel the unstarted primary, switch ----------
            record["cutover_fired"] = True
            cancel_task = asyncio.create_task(
                safe_cancel(primary_handle, reason="still queued at cutover poll")
            )
            hedge_handle = await hedge.submit()
            record["hedge_submitted_at_ms"] = round(now_ms() - t0, 1)
            hedge_task = asyncio.create_task(_resolve(hedge_handle, cap))
            # The primary's result task STAYS in the race: if the remote
            # cancel failed/leaked, a successfully cancelled job simply never
            # produces a valid result, while a leaked one can still rescue a
            # failing hedge.
            pending = {primary_task, hedge_task}
            while pending and winner_result is None:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                winner_key, winner_result = _first_valid(done, validator)
            receipts.append(await cancel_task)
            cancel_task = None
            # If the (supposedly cancelled) primary won after all, the hedge
            # is now the loser and must be stopped too.
            if winner_key == primary_key and hedge_task is not None \
                    and not hedge_task.done():
                try:
                    await hedge_handle.status()
                except Exception:  # noqa: BLE001 - best-effort
                    pass
                receipts.append(
                    await safe_cancel(hedge_handle, reason="lost the race")
                )
        else:
            # -------- keep the primary; arm the safety hedge -----------------
            elapsed_s = (now_ms() - t0) / 1000.0
            done, _ = await asyncio.wait(
                {primary_task}, timeout=max(0.0, safety_s - elapsed_s)
            )
            if done:
                _, result = primary_task.result()
                if _valid(result):
                    winner_key, winner_result = primary_key, result
            if winner_result is None:
                record["safety_hedge_fired"] = True
                hedge_handle = await hedge.submit()
                record["hedge_submitted_at_ms"] = round(now_ms() - t0, 1)
                hedge_task = asyncio.create_task(_resolve(hedge_handle, cap))
                pending = {primary_task, hedge_task}
                while pending and winner_result is None:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    winner_key, winner_result = _first_valid(done, validator)
                # cancel the loser (poll its state first so the receipt's
                # was_running is meaningful)
                if winner_key is not None:
                    loser_handle = (
                        hedge_handle if winner_key == primary_key else primary_handle
                    )
                    loser_task = hedge_task if winner_key == primary_key else primary_task
                    if loser_task is not None and not loser_task.done():
                        try:
                            await loser_handle.status()
                        except Exception:  # noqa: BLE001 - best-effort
                            pass
                        receipts.append(
                            await safe_cancel(loser_handle, reason="lost the race")
                        )

    for task in (primary_task, hedge_task, cancel_task):
        if task is not None and not task.done():
            task.cancel()

    if winner_result is not None and winner_key is not None:
        ledger.charge(config.provider(winner_key), winner_result.wall_s,
                      stage=stage, note="winner")
    for receipt in receipts:
        if receipt.estimated_cost_usd:
            ledger.charge_usd(receipt.provider, receipt.estimated_cost_usd,
                              stage=stage, note="loser before cancel")

    # End-to-end latency from request start (a hedge win adds its launch offset).
    winner_total_ms = None
    if winner_result is not None and winner_key is not None:
        winner_handle = primary_handle if winner_key == primary_key else hedge_handle
        if winner_handle is not None:
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
