"""One paired three-provider cold-start round (Stage 2 of the plan).

The same request is launched concurrently on every provider; all are allowed to
finish (or hit the 300 s cap and be right-censored) so we capture complete
lifecycle traces for offline policy replay. Immediately after each provider's
cold request, an optional warm-companion request is issued to the same
container to decompose cold = queue + container + load + first-gen vs
warm ≈ generation (benchmarks/2026-07-moss/methodology.md).

This module does NOT hedge or cancel — that is Stage 3 (live_hedge.py). Here we
collect the ground-truth paired traces that let hundreds of policies be
simulated without spending more GPU money.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from gpuhedge.backends import Backend, ProviderResult
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import CostLedger, TraceWriter, utc_now_iso
from gpuhedge.validators import validate_wav


@dataclass
class ProviderRoundOutcome:
    provider: str
    state: str
    wall_s: float
    valid: bool
    validation_reasons: list[str]
    projected_cost_usd: float
    warm_wall_s: float | None = None
    provider_metrics: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class RoundResult:
    round_id: int
    block: int
    stage: str
    outcomes: dict[str, ProviderRoundOutcome]
    winner: str | None
    winner_wall_s: float | None
    projected_round_cost_usd: float

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": "round",
            "stage": self.stage,
            "round_id": self.round_id,
            "block": self.block,
            "winner": self.winner,
            "winner_wall_s": self.winner_wall_s,
            "projected_round_cost_usd": round(self.projected_round_cost_usd, 6),
            "providers": {
                key: {
                    "state": o.state,
                    "wall_s": round(o.wall_s, 2),
                    "valid": o.valid,
                    "validation_reasons": o.validation_reasons,
                    "warm_wall_s": o.warm_wall_s,
                    "projected_cost_usd": round(o.projected_cost_usd, 6),
                    "metrics": o.provider_metrics,
                    "events": o.events,
                    "error": o.error,
                }
                for key, o in self.outcomes.items()
            },
        }


async def _run_one_provider(
    backend: Backend,
    config: BenchmarkConfig,
    ledger: CostLedger,
    *,
    stage: str,
    timeout_s: float,
    warm_companion: bool,
) -> ProviderRoundOutcome:
    expected_sr = None  # MOSS sr is provider-reported; duration/RMS gates suffice
    try:
        handle = await backend.submit()
    except Exception as exc:  # noqa: BLE001 - NotDeployedError etc.
        return ProviderRoundOutcome(
            provider=backend.key, state="SUBMIT_FAILED", wall_s=0.0, valid=False,
            validation_reasons=[str(exc)[:200]], projected_cost_usd=0.0, error=str(exc)[:400],
        )

    result: ProviderResult = await handle.result(timeout_s)
    validation = validate_wav(result.audio, expected_sample_rate=expected_sr)

    # Projected cost for the cold request (charge enforces budget gates).
    cost = ledger.charge(
        backend.provider, billed_seconds=result.wall_s, stage=stage,
        note=f"cold {result.state.value}",
    )

    warm_wall: float | None = None
    if warm_companion and result.ok:
        try:
            warm_handle = await backend.submit()
            warm_result = await warm_handle.result(timeout_s)
            warm_wall = warm_result.wall_s
            cost += ledger.charge(
                backend.provider, billed_seconds=warm_result.wall_s, stage=stage,
                note="warm companion", include_idle=False,
            )
        except Exception:  # noqa: BLE001 - warm sample is best-effort
            warm_wall = None

    return ProviderRoundOutcome(
        provider=backend.key,
        state=result.state.value,
        wall_s=result.wall_s,
        valid=validation.valid,
        validation_reasons=validation.reasons,
        projected_cost_usd=cost,
        warm_wall_s=warm_wall,
        provider_metrics=result.provider_metrics,
        events=[e.__dict__ for e in result.events],
        error=result.error,
    )


async def run_paired_round(
    config: BenchmarkConfig,
    backends: list[Backend],
    ledger: CostLedger,
    trace: TraceWriter,
    *,
    round_id: int,
    block: int,
    stage: str = "moss",
    timeout_s: float | None = None,
    warm_companion: bool = True,
) -> RoundResult:
    """Launch the request on all backends concurrently and record the round."""

    cap = timeout_s if timeout_s is not None else config.moss_timeout_s()
    started = utc_now_iso()

    outcomes_list = await asyncio.gather(
        *(
            _run_one_provider(
                b, config, ledger, stage=stage, timeout_s=cap, warm_companion=warm_companion
            )
            for b in backends
        )
    )
    outcomes = {o.provider: o for o in outcomes_list}

    valid = [(o.provider, o.wall_s) for o in outcomes_list if o.valid]
    winner, winner_wall = min(valid, key=lambda kv: kv[1]) if valid else (None, None)
    round_cost = sum(o.projected_cost_usd for o in outcomes_list)

    result = RoundResult(
        round_id=round_id, block=block, stage=stage, outcomes=outcomes,
        winner=winner, winner_wall_s=winner_wall, projected_round_cost_usd=round_cost,
    )
    record = result.to_record()
    record["started"] = started
    trace.write(record)
    return result
