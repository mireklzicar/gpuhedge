"""Projected-cost ledger with the hard budget gates from the $50 plan.

Every request appends a projected charge; the running total is checked against
the operational stop ($45), the absolute ceiling ($50), and the per-stage gates
(docs/cost-accounting.md). Crossing a limit raises
``BudgetExceeded`` so the driver stops submitting rather than discovering the
overspend on a provider dashboard that lags. Reconciliation against
provider-reported cost is recorded after every six-round block.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpuhedge.config import BenchmarkConfig, Provider
from gpuhedge.telemetry.trace import TraceWriter, read_traces, utc_now_iso


class BudgetExceeded(RuntimeError):
    def __init__(self, message: str, *, projected: float, limit: float, limit_name: str) -> None:
        super().__init__(message)
        self.projected = projected
        self.limit = limit
        self.limit_name = limit_name


@dataclass
class LedgerEntry:
    stage: str
    provider: str
    projected_usd: float
    billed_seconds: float | None
    note: str
    ts: str


class CostLedger:
    """Resumable projected-cost accumulator. Reloads prior entries from its
    JSONL file on construction so a re-run continues from the real spend."""

    def __init__(self, config: BenchmarkConfig, ledger_path: str | Path | None = None) -> None:
        self.config = config
        self.budget = config.budget
        path = ledger_path or (config.trace_dir() / "ledger.jsonl")
        self.path = Path(path)
        self._writer = TraceWriter(self.path)
        self.entries: list[LedgerEntry] = []
        self.reconciled: dict[str, float] = {}
        self._projected_total = 0.0
        self._load_existing()

    # ------------------------------------------------------------- persistence
    def _load_existing(self) -> None:
        if not self.path.is_file():
            return
        for rec in read_traces(self.path):
            if rec.get("kind") == "charge":
                self._projected_total += float(rec.get("projected_usd", 0.0))
                self.entries.append(
                    LedgerEntry(
                        stage=rec.get("stage", "?"),
                        provider=rec.get("provider", "?"),
                        projected_usd=float(rec.get("projected_usd", 0.0)),
                        billed_seconds=rec.get("billed_seconds"),
                        note=rec.get("note", ""),
                        ts=rec.get("ts", ""),
                    )
                )
            elif rec.get("kind") == "reconcile":
                self.reconciled[rec["provider"]] = float(rec.get("actual_usd", 0.0))

    # -------------------------------------------------------------- accounting
    @property
    def projected_total(self) -> float:
        return round(self._projected_total, 6)

    def _append(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
        self._projected_total += entry.projected_usd
        self._writer.write({"kind": "charge", **entry.__dict__})

    def charge(
        self,
        provider: Provider,
        billed_seconds: float,
        *,
        stage: str,
        note: str = "",
        include_idle: bool = True,
    ) -> float:
        """Record a projected charge for one provider job and enforce limits."""

        cost = provider.billed_cost(billed_seconds, include_idle=include_idle)
        self._append(
            LedgerEntry(
                stage=stage, provider=provider.key, projected_usd=cost,
                billed_seconds=round(billed_seconds, 2), note=note, ts=utc_now_iso(),
            )
        )
        self._enforce(stage)
        return cost

    def charge_usd(self, provider_key: str, usd: float, *, stage: str, note: str = "") -> float:
        self._append(
            LedgerEntry(
                stage=stage, provider=provider_key, projected_usd=round(usd, 6),
                billed_seconds=None, note=note, ts=utc_now_iso(),
            )
        )
        self._enforce(stage)
        return usd

    # ----------------------------------------------------------------- gating
    def _enforce(self, stage: str) -> None:
        total = self._projected_total
        if total > self.budget.absolute_ceiling:
            raise BudgetExceeded(
                f"projected spend ${total:.2f} exceeds absolute ceiling "
                f"${self.budget.absolute_ceiling:.2f} — HARD STOP",
                projected=total, limit=self.budget.absolute_ceiling,
                limit_name="absolute_ceiling",
            )
        if total > self.budget.operational_stop:
            raise BudgetExceeded(
                f"projected spend ${total:.2f} exceeds operational stop "
                f"${self.budget.operational_stop:.2f} (${self.budget.absolute_ceiling:.2f} "
                "ceiling left for billing lag) — stop submitting",
                projected=total, limit=self.budget.operational_stop,
                limit_name="operational_stop",
            )

    def check_gate(self, gate_name: str) -> None:
        """Assert the cumulative projected spend is within a named stage gate."""

        limit = self.budget.gates.get(gate_name)
        if limit is None:
            raise KeyError(f"unknown budget gate {gate_name!r}; have {sorted(self.budget.gates)}")
        if self._projected_total > limit:
            raise BudgetExceeded(
                f"gate {gate_name!r}: projected ${self._projected_total:.2f} > ${limit:.2f}",
                projected=self._projected_total, limit=limit, limit_name=gate_name,
            )

    def remaining_to_gate(self, gate_name: str) -> float:
        return round(self.budget.gates[gate_name] - self._projected_total, 6)

    def remaining_to_stop(self) -> float:
        return round(self.budget.operational_stop - self._projected_total, 6)

    def would_exceed_stop(self, extra_usd: float) -> bool:
        return (self._projected_total + extra_usd) > self.budget.operational_stop

    # ----------------------------------------------------------- reconciliation
    def reconcile(self, provider_key: str, actual_usd: float, *, note: str = "") -> None:
        """Record provider-reported actual cost (run after each six-round block)."""

        self.reconciled[provider_key] = actual_usd
        self._writer.write(
            {"kind": "reconcile", "provider": provider_key,
             "actual_usd": round(actual_usd, 6), "note": note, "ts": utc_now_iso()}
        )

    # --------------------------------------------------------------- reporting
    def by_provider(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for e in self.entries:
            totals[e.provider] = round(totals.get(e.provider, 0.0) + e.projected_usd, 6)
        return totals

    def by_stage(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for e in self.entries:
            totals[e.stage] = round(totals.get(e.stage, 0.0) + e.projected_usd, 6)
        return totals

    def summary(self) -> dict[str, Any]:
        return {
            "projected_total_usd": self.projected_total,
            "operational_stop_usd": self.budget.operational_stop,
            "absolute_ceiling_usd": self.budget.absolute_ceiling,
            "remaining_to_stop_usd": self.remaining_to_stop(),
            "by_provider": self.by_provider(),
            "by_stage": self.by_stage(),
            "reconciled": self.reconciled,
            "entries": len(self.entries),
        }

    def close(self) -> None:
        self._writer.close()
