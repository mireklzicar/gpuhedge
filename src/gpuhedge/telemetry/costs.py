"""Actual-cost monitoring — what providers *report*, alongside the projected ledger.

The ledger (`ledger.py`) projects spend from wall time x configured rates. This
module tracks provider-reported truth so the two can be reconciled per block:

- **RunPod**: account balance via the public GraphQL API (``myself.clientBalance``,
  real-time). Actual spend = baseline balance - current balance. Caveat: the
  delta includes unrelated account billing (e.g. network-volume storage at
  ~$0.005/hr), noted in the reading detail.
- **Modal**: ``Workspace.billing.report`` — per-app cost (CPU/Memory/GPU split)
  in daily buckets that update intraday. Actual spend = sum of the app's
  buckets since monitoring started, minus the baseline.
- **Cerebrium**: no public balance/usage API found (2026-07; ``rest.cerebrium.ai``
  serves projects/apps only). Readings are marked ``unavailable`` — reconcile
  from the dashboard, and rely on per-run durations captured in traces.

Snapshots are appended to ``traces/costs.jsonl`` (baseline = first snapshot,
reloaded on restart so the monitor is resumable like the ledger).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry.trace import TraceWriter, read_traces, utc_now_iso

_RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"


@dataclass
class CostReading:
    provider: str
    kind: str                    # "balance" | "app_cost" | "unavailable"
    value_usd: float | None      # balance for RunPod; cost-to-date for Modal
    detail: str = ""


# ------------------------------------------------------------------ readers
def read_runpod_balance() -> CostReading:
    """Current account credit via GraphQL (real-time)."""

    import requests

    from gpuhedge.backends.runpod_backend import load_runpod_api_key

    try:
        resp = requests.post(
            _RUNPOD_GRAPHQL,
            headers={"Authorization": f"Bearer {load_runpod_api_key()}"},
            json={"query": "query { myself { clientBalance currentSpendPerHr } }"},
            timeout=20,
        )
        resp.raise_for_status()
        myself = resp.json()["data"]["myself"]
        return CostReading(
            "runpod", "balance", float(myself["clientBalance"]),
            f"currentSpendPerHr={myself.get('currentSpendPerHr')} "
            "(delta includes non-benchmark billing, e.g. volume storage)",
        )
    except Exception as exc:  # noqa: BLE001
        return CostReading("runpod", "unavailable", None, str(exc)[:200])


def read_modal_app_cost(app_name: str, since: datetime | None = None) -> CostReading:
    """Cost-to-date for one Modal app via ``Workspace.billing.report``.

    Daily buckets update intraday; sums every bucket from ``since``'s UTC day
    through tomorrow so day-boundary crossings during a benchmark are covered.
    """

    try:
        import modal

        ws = modal.Workspace.from_context()
        now = datetime.now(timezone.utc)
        start_day = (since or now).astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = ws.billing.report(start=start_day, end=now + timedelta(days=1))
        total = sum(float(r.cost) for r in rows if r.description == app_name)
        return CostReading(
            "modal", "app_cost", round(total, 6),
            f"app={app_name} buckets_from={start_day.date()} (intraday, may lag minutes)",
        )
    except Exception as exc:  # noqa: BLE001
        return CostReading("modal", "unavailable", None, str(exc)[:200])


def read_cerebrium() -> CostReading:
    return CostReading(
        "cerebrium", "unavailable", None,
        "no public balance/usage API (2026-07) — reconcile from dashboard; "
        "per-run durations are captured in traces",
    )


# ------------------------------------------------------------------ monitor
class CostMonitor:
    """Resumable block-boundary snapshots of provider-reported cost.

    ``snapshot(label)`` records all readings and returns actual-spend deltas vs
    the baseline (first-ever snapshot), so the controller can print
    projected-vs-actual after every block.
    """

    def __init__(
        self,
        config: BenchmarkConfig,
        path: str | Path | None = None,
        readers: dict[str, Callable[[], CostReading]] | None = None,
    ) -> None:
        self.config = config
        self.path = Path(path) if path else (config.trace_dir() / "costs.jsonl")
        self._writer = TraceWriter(self.path)
        modal_provider = config.providers.get("modal")
        modal_app_name = (
            modal_provider.extra.get("app", "gpuhedge-moss")
            if modal_provider
            else "gpuhedge-moss"
        )
        self.baseline: dict[str, float] = {}
        self.baseline_ts: str | None = None
        self._load_baseline()
        since = (
            datetime.fromisoformat(self.baseline_ts)
            if self.baseline_ts
            else datetime.now(timezone.utc)
        )
        self.readers = readers or {
            "runpod": read_runpod_balance,
            "modal": lambda: read_modal_app_cost(modal_app_name, since=since),
            "cerebrium": read_cerebrium,
        }

    def _load_baseline(self) -> None:
        if not self.path.is_file():
            return
        for rec in read_traces(self.path):
            if rec.get("kind") == "cost_snapshot" and rec.get("is_baseline"):
                self.baseline = {
                    p: r["value_usd"]
                    for p, r in rec.get("readings", {}).items()
                    if r.get("value_usd") is not None
                }
                self.baseline_ts = rec.get("ts")
                return

    @staticmethod
    def _actual_spend(provider: str, kind: str, baseline: float, current: float) -> float:
        # Balances decrease as money is spent; app-cost counters increase.
        return round(baseline - current if kind == "balance" else current - baseline, 6)

    def snapshot(self, label: str, *, projected_total: float | None = None) -> dict[str, Any]:
        readings = {key: reader() for key, reader in self.readers.items()}
        is_baseline = not self.baseline
        if is_baseline:
            self.baseline = {
                p: r.value_usd for p, r in readings.items() if r.value_usd is not None
            }
            self.baseline_ts = utc_now_iso()

        actual: dict[str, float] = {}
        for provider, reading in readings.items():
            base = self.baseline.get(provider)
            if base is not None and reading.value_usd is not None:
                actual[provider] = self._actual_spend(
                    provider, reading.kind, base, reading.value_usd
                )

        record = {
            "kind": "cost_snapshot",
            "label": label,
            "is_baseline": is_baseline,
            "readings": {p: asdict(r) for p, r in readings.items()},
            "actual_spend_since_baseline": actual,
            "projected_total_usd": projected_total,
        }
        self._writer.write(record)
        return record

    def close(self) -> None:
        self._writer.close()


def format_snapshot(record: dict[str, Any]) -> str:
    """One-line summary for the controller log."""

    parts = []
    for provider, reading in record.get("readings", {}).items():
        value = reading.get("value_usd")
        if value is None:
            parts.append(f"{provider}=n/a")
        else:
            spent = record.get("actual_spend_since_baseline", {}).get(provider)
            unit = "bal" if reading.get("kind") == "balance" else "cost"
            spent_txt = f" spent≈${spent:.2f}" if spent is not None else ""
            parts.append(f"{provider} {unit}=${value:.2f}{spent_txt}")
    projected = record.get("projected_total_usd")
    tail = f" | ledger projected=${projected:.2f}" if projected is not None else ""
    return " | ".join(parts) + tail
