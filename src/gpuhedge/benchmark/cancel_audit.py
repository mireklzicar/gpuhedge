"""Deliberate loser-cancellation audit (docs/cancellation-semantics.md).

Stage 3 only ever cancelled RunPod (Modal and Cerebrium kept winning), so the
"losers really stop" claim had one live-audited provider. This audit forces
each provider to be the loser, twice per phase:

- ``early``  — cancel ~1 s after submit, while the job should still be queued
               (on RunPod the fast-path queue alone is 1.1-2.0 s). The billing
               question: does a never-started job bill anything?
- ``late``   — poll until the job reports RUNNING, then cancel ~1.5 s later
               (mid-generation on a fast path, mid-load on a cold worker).
               Providers whose status cannot distinguish queued from running
               (Modal) use a fixed mid-cold-start delay instead. The billing
               question: does billing stop at cancel?

Each cancel produces a full CancellationReceipt plus the provider-reported
billing evidence available (RunPod ``executionTime``; Cerebrium run status;
Modal has no per-call billing surface — its evidence is the billing API delta,
reconciled at the block level). Jobs are spaced past every scale-down window
so each starts cold.

Output: one JSONL record per cancel in ``traces/cancel_audit.jsonl`` and a
capability-matrix summary record at the end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from gpuhedge.backends import build_backend
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import CostLedger, TraceWriter

Logger = Callable[[str], None]

EARLY_DELAY_S = 1.0

# "late" strategy per provider: wait for a RUNNING status then strike, or a
# fixed delay where lifecycle states are not observable pre-completion.
LATE_STRATEGY: dict[str, dict[str, Any]] = {
    # IN_QUEUE/IN_PROGRESS are real states: poll, then cancel 1.5 s into RUNNING
    "runpod": {"mode": "wait_running", "poll_s": 0.5, "grace_s": 1.5,
               "max_wait_s": 35.0, "fallback_delay_s": 25.0},
    # Modal FunctionCall cannot distinguish queued from running -> fixed delay
    # mid cold start (cold p50 was ~38 s)
    "modal": {"mode": "fixed", "delay_s": 15.0},
    # Cerebrium runs-list shows "processing" only; fixed delay mid load
    # (cold path ~19 s)
    "cerebrium": {"mode": "fixed", "delay_s": 10.0},
}


async def run_cancel_audit(
    config: BenchmarkConfig,
    *,
    providers: list[str] | None = None,
    repeats: int = 2,
    inter_job_wait_s: float = 130.0,
    log: Logger = print,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    keys = providers or list(config.providers)
    ledger = CostLedger(config)
    trace = TraceWriter(config.trace_dir() / "cancel_audit.jsonl")
    matrix: dict[str, dict[str, dict[str, int]]] = {}

    log(f"Cancellation audit: {keys} x (early, late) x {repeats}, "
        f"{inter_job_wait_s:.0f}s cold gaps")
    try:
        first = True
        for key in keys:
            backend = build_backend(config.provider(key), config.request)
            matrix[key] = {}
            for phase in ("early", "late"):
                cell = {"attempts": 0, "terminal": 0, "leaked": 0, "was_running": 0}
                matrix[key][phase] = cell
                for i in range(repeats):
                    if not first:
                        await sleep(inter_job_wait_s)
                    first = False
                    rec = await _one_forced_cancel(
                        backend, config, ledger, phase=phase, log=log, sleep=sleep,
                    )
                    rec.update({"kind": "forced_cancel", "provider": key,
                                "phase": phase, "attempt": i + 1})
                    trace.write(rec)
                    cell["attempts"] += 1
                    receipt = rec.get("receipt") or {}
                    cell["terminal"] += int(receipt.get("terminal_status") is not None
                                            and not receipt.get("leaked"))
                    cell["leaked"] += int(bool(receipt.get("leaked")))
                    cell["was_running"] += int(bool(receipt.get("was_running")))
        summary = {"kind": "cancel_audit_summary", "matrix": matrix,
                   "projected_spend_usd": round(ledger.projected_total, 4)}
        trace.write(summary)
        return summary
    finally:
        trace.close()
        ledger.close()


async def _one_forced_cancel(
    backend, config: BenchmarkConfig, ledger: CostLedger, *,
    phase: str, log: Logger, sleep,
) -> dict[str, Any]:
    from gpuhedge.backends import JobState
    from gpuhedge.backends.base import now_ms

    handle = await backend.submit()
    t0 = now_ms()

    if phase == "early":
        await sleep(EARLY_DELAY_S)
    else:
        strat = LATE_STRATEGY.get(backend.key, LATE_STRATEGY["runpod"])
        if strat["mode"] == "fixed":
            await sleep(float(strat["delay_s"]))
        else:
            deadline = t0 + float(strat["max_wait_s"]) * 1000.0
            running = False
            while now_ms() < deadline:
                try:
                    if (await handle.status()) is JobState.IN_PROGRESS:
                        running = True
                        break
                except Exception:  # noqa: BLE001 - keep polling
                    pass
                await sleep(float(strat["poll_s"]))
            if running:
                await sleep(float(strat["grace_s"]))
            else:  # never observed RUNNING within the window
                await sleep(max(0.0, float(strat["fallback_delay_s"])
                                - (now_ms() - t0) / 1000.0))

    state_before = None
    try:
        state_before = (await handle.status()).value
    except Exception as exc:  # noqa: BLE001 - providers without cheap status
        state_before = f"status_error:{str(exc)[:80]}"
    cancel_at_s = (now_ms() - t0) / 1000.0
    receipt = await handle.cancel(reason=f"cancel_audit:{phase}")
    if receipt.estimated_cost_usd:
        ledger.charge_usd(backend.provider.key, receipt.estimated_cost_usd,
                          stage="cancel_audit", note=f"{phase} cancel")
    log(f"  {backend.key} {phase}@{cancel_at_s:.1f}s: state_before={state_before} "
        f"terminal={receipt.terminal_status} leaked={receipt.leaked} "
        f"ack={receipt.ack_latency_ms()}ms exec_before_cancel="
        f"{receipt.execution_ms_before_cancel}")
    return {
        "cancel_at_s": round(cancel_at_s, 2),
        "state_before_cancel": state_before,
        "receipt": asdict(receipt),
    }
