"""Routing policies — declarative values that know how to execute themselves.

A policy says WHEN additional jobs launch and HOW the race resolves. The
Router dispatches through one protocol method, ``execute(ctx)``, so custom
policies are first-class: implement ``execute`` (and declare how many
providers you need via ``min_providers``) and the Router runs yours exactly
like the built-ins — no isinstance checks anywhere.

    SingleProvider()                     primary only, no hedging
    FixedHedgePolicy(hedge_after_ms)     timer hedge: launch the backup at t=d
    StateAwarePolicy(queue_cutover_ms,   poll the primary's queue state; cancel
                     safety_hedge_ms)    an unstarted primary and switch early
    CascadePolicy(queue_cutover_ms,      state-aware cutover + a second-level
                  safety_hedge_ms,       fallback when the hedge itself enters
                  escalate_after_ms)     a tail; never more than two live jobs

The engines in ``gpuhedge.benchmark`` do the actual submitting/racing/
cancelling against real (or simulated) providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles
    from gpuhedge.config import BenchmarkConfig
    from gpuhedge.telemetry import CostLedger, TraceWriter


@dataclass
class RoutingContext:
    """Everything a policy needs to run one request end to end."""

    config: BenchmarkConfig
    ledger: CostLedger
    trace: TraceWriter
    providers: tuple[str, ...]      # (primary, hedge?, fallback?, ...)
    request_id: int = 0
    timeout_s: float | None = None
    stage: str = "router"

    @property
    def primary(self) -> str:
        return self.providers[0]

    @property
    def hedge(self) -> str:
        return self.providers[1]

    @property
    def fallback(self) -> str:
        return self.providers[2]


@runtime_checkable
class Policy(Protocol):
    """The public extension point: any object with ``min_providers`` and an
    async ``execute`` that returns the engine's trace record."""

    min_providers: int

    async def execute(self, ctx: RoutingContext) -> dict[str, Any]:
        ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class SingleProvider:
    """No hedging — the baseline the benchmark measures everything against."""

    min_providers = 1

    async def execute(self, ctx: RoutingContext) -> dict[str, Any]:
        from gpuhedge.benchmark.validation import single_provider_request

        cap = ctx.timeout_s if ctx.timeout_s is not None else ctx.config.moss_timeout_s()
        return await single_provider_request(
            ctx.config, ctx.ledger, ctx.trace, provider_key=ctx.primary,
            stage=ctx.stage, timeout_s=cap, request_id=ctx.request_id)


@dataclass(frozen=True)
class FixedHedgePolicy:
    """Launch the hedge if no valid result after ``hedge_after_ms``."""

    hedge_after_ms: int = 10_000
    min_providers = 2

    async def execute(self, ctx: RoutingContext) -> dict[str, Any]:
        from gpuhedge.benchmark.live_hedge import run_hedged_request

        return await run_hedged_request(
            ctx.config, ctx.ledger, ctx.trace,
            primary_key=ctx.primary, hedge_key=ctx.hedge,
            hedge_after_ms=self.hedge_after_ms, timeout_s=ctx.timeout_s,
            request_id=ctx.request_id)


@dataclass(frozen=True)
class StateAwarePolicy:
    """Poll the primary's lifecycle state at ``queue_cutover_ms``: still
    queued -> cancel it before its worker starts and switch to the hedge;
    running -> keep it, with a safety hedge at ``safety_hedge_ms``."""

    queue_cutover_ms: int = 2_500
    safety_hedge_ms: int = 8_500
    min_providers = 2

    async def execute(self, ctx: RoutingContext) -> dict[str, Any]:
        from gpuhedge.benchmark.state_aware import run_state_aware_request

        return await run_state_aware_request(
            ctx.config, ctx.ledger, ctx.trace,
            primary_key=ctx.primary, hedge_key=ctx.hedge,
            queue_cutover_ms=self.queue_cutover_ms,
            safety_hedge_ms=self.safety_hedge_ms, timeout_s=ctx.timeout_s,
            request_id=ctx.request_id, stage=ctx.stage)


@dataclass(frozen=True)
class CascadePolicy:
    """State-aware cutover with a second-level fallback: the hedge provider
    can have its own tail (observed live: one 104 s hedge outlier), so at
    ``escalate_after_ms`` from request start, if no valid result has arrived
    and fewer than two attempts are still live, the fallback provider is
    launched. At most two GPU jobs run at any moment."""

    queue_cutover_ms: int = 2_500
    safety_hedge_ms: int = 8_500
    escalate_after_ms: int = 25_000
    min_providers = 3

    async def execute(self, ctx: RoutingContext) -> dict[str, Any]:
        from gpuhedge.benchmark.cascade import run_cascade_request

        return await run_cascade_request(
            ctx.config, ctx.ledger, ctx.trace,
            primary_key=ctx.primary, hedge_key=ctx.hedge,
            fallback_key=ctx.fallback,
            queue_cutover_ms=self.queue_cutover_ms,
            safety_hedge_ms=self.safety_hedge_ms,
            escalate_after_ms=self.escalate_after_ms, timeout_s=ctx.timeout_s,
            request_id=ctx.request_id, stage=ctx.stage)


__all__ = [
    "Policy",
    "RoutingContext",
    "SingleProvider",
    "FixedHedgePolicy",
    "StateAwarePolicy",
    "CascadePolicy",
]
