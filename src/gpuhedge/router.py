"""The small public API: race deployments under a policy, get the first valid
result, and an audited cancellation receipt for every loser.

    from gpuhedge import Router
    from gpuhedge.policies import StateAwarePolicy

    # stable mode (default): 10 s timer hedge
    router = Router(primary="runpod", hedge="cerebrium")

    # experimental mode: cut over at 2.5 s — lower typical latency, more tail risk
    router = Router(
        primary="runpod", hedge="cerebrium",
        policy=StateAwarePolicy(queue_cutover_ms=2_500, safety_hedge_ms=8_500),
    )
    outcome = await router.run()
    outcome.winner, outcome.total_ms, outcome.cancellation

Providers come from the benchmark config (packaged default, a local
``./config/benchmark.yaml``, or an explicit path/object) — including
simulated (``adapter: sim``) and generic HTTP (``adapter: http``) providers,
so the Router runs identically with or without cloud accounts.

Policies are dispatched through one protocol method — ``execute(ctx)`` — so
a custom policy class runs exactly like the built-ins (docs/policies.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpuhedge.config import BenchmarkConfig, load_config
from gpuhedge.policies import FixedHedgePolicy, Policy, RoutingContext
from gpuhedge.telemetry import CostLedger, TraceWriter


@dataclass
class RouterOutcome:
    winner: str | None
    total_ms: float | None                 # end-to-end from request start
    hedged: bool                           # a second (or third) job was launched
    cancellation: dict[str, Any] | None    # the loser's receipt, if any
    audio: bytes | None = None             # winning provider's decoded WAV bytes
    sample_rate: int | None = None
    metrics: dict[str, Any] | None = None  # winner's provider_metrics
    record: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.winner is not None


class Router:
    def __init__(
        self,
        *,
        primary: str,
        hedge: str | None = None,
        fallback: str | None = None,
        policy: Policy | None = None,
        config: BenchmarkConfig | str | Path | None = None,
        request: dict[str, Any] | None = None,
        trace_path: str | Path | None = None,
    ) -> None:
        if isinstance(config, BenchmarkConfig):
            self.config = config
        else:
            self.config = load_config(config)
        if request:
            self.config.request.update(request)
        self.providers = tuple(
            key for key in (primary, hedge, fallback) if key is not None
        )
        self.policy: Policy = policy or FixedHedgePolicy()
        needed = getattr(self.policy, "min_providers", 2)
        if len(self.providers) < needed:
            raise ValueError(
                f"{type(self.policy).__name__} needs {needed} provider(s) "
                f"(primary/hedge/fallback); got {self.providers}"
            )
        self._trace = TraceWriter(
            Path(trace_path) if trace_path
            else self.config.trace_dir() / "router.jsonl"
        )
        self._ledger = CostLedger(self.config)
        self._counter = 0

    async def run(
        self, request_id: int | None = None, *, timeout_s: float | None = None
    ) -> RouterOutcome:
        """Route one request; first valid result wins; losers are cancelled
        with an audited receipt (evidence level, scope, leak flag).

        ``timeout_s`` caps each attempt's wait (per provider); omit to use the
        model's benchmark cap from config (``timeouts_s.moss``, default 300 s).
        A web caller typically passes a much shorter deadline."""

        self._counter += 1
        rid = request_id if request_id is not None else self._counter
        ctx = RoutingContext(
            config=self.config, ledger=self._ledger, trace=self._trace,
            providers=self.providers, request_id=rid, stage="router",
            timeout_s=timeout_s,
        )
        record = await self.policy.execute(ctx)

        return RouterOutcome(
            winner=record.get("winner"),
            total_ms=record.get("winner_total_ms"),
            hedged=bool(record.get("hedge_launched") or record.get("cutover_fired")
                        or record.get("safety_hedge_fired")
                        or record.get("escalation_fired")),
            cancellation=record.get("cancellation"),
            audio=record.get("_winner_audio"),
            sample_rate=record.get("_winner_sample_rate"),
            metrics=record.get("winner_metrics"),
            record={k: v for k, v in record.items() if not k.startswith("_")},
        )

    def close(self) -> None:
        self._trace.close()
        self._ledger.close()

    def __enter__(self) -> Router:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
