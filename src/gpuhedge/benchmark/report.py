"""Human-readable report over collected Stage 2 traces.

`gpuhedge report` prints, from traces/moss_rounds.jsonl:
- the cold-start matrix (round x provider, bucket-coloured),
- per-provider stats (p50/p95/max, deadline miss rates, valid rate),
- winner distribution,
- the offline policy sweep (replay.py) on calibration vs evaluation splits,
- ledger + latest cost snapshot.

Publishing rule from the plan: report p50/p95/empirical max and fixed-deadline
miss rates; never a p99 from tens of samples.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from gpuhedge.benchmark.replay import Round, load_rounds, standard_policy_sweep
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry.trace import read_traces

INF = math.inf


def _bucket(latency: float) -> str:
    if math.isinf(latency):
        return "[black on red]CENS[/]"
    if latency < 30:
        return f"[green]{latency:5.1f}[/green]"
    if latency < 60:
        return f"[yellow]{latency:5.1f}[/yellow]"
    if latency < 120:
        return f"[orange3]{latency:5.1f}[/orange3]"
    return f"[red]{latency:5.1f}[/red]"


def print_report(
    config: BenchmarkConfig,
    console: Console,
    rounds_path: str | Path,
    *,
    ledger_summary: dict[str, Any] | None = None,
    latest_snapshot: dict[str, Any] | None = None,
) -> None:
    rounds = load_rounds(rounds_path)
    if not rounds:
        console.print(f"[yellow]no rounds in {rounds_path}[/yellow]")
        return
    providers = list(config.providers)
    deadlines = [int(d) for d in config.slo.get("report_deadlines_s", [30, 60, 90, 120])]

    # ---------------------------------------------------------------- matrix
    matrix = Table(title=f"Cold-start matrix ({len(rounds)} rounds)", header_style="bold")
    matrix.add_column("round", justify="right")
    matrix.add_column("block", justify="right")
    for p in providers:
        matrix.add_column(p, justify="right")
    matrix.add_column("winner")
    for r in rounds:
        finite = {p: v for p, v in r.latency.items() if not math.isinf(v)}
        winner = min(finite, key=finite.get) if finite else "-"
        matrix.add_row(
            str(r.round_id), str(r.block),
            *[_bucket(r.latency.get(p, INF)) for p in providers],
            winner,
        )
    console.print(matrix)

    # ----------------------------------------------------------- provider stats
    stats = Table(title="Per-provider cold starts", header_style="bold")
    for col in ("provider", "n", "valid", "p50 s", "p95 s", "max s",
                *[f"miss>{d}s" for d in deadlines]):
        stats.add_column(col, justify="right")
    def pct(finite: list[float], q: float) -> str:
        if not finite:
            return "-"
        return f"{finite[min(len(finite) - 1, int(round(q * (len(finite) - 1))))]:.1f}"

    for p in providers:
        lats = [r.latency.get(p, INF) for r in rounds]
        finite = sorted(x for x in lats if not math.isinf(x))
        n = len(lats)
        stats.add_row(
            p, str(n), f"{len(finite)}/{n}", pct(finite, 0.5), pct(finite, 0.95),
            f"{finite[-1]:.1f}" if finite else "-",
            *[f"{sum(1 for x in lats if x > d) / n:.0%}" for d in deadlines],
        )
    console.print(stats)

    # ------------------------------------------------------------ policy sweep
    for label, subset in _splits(config, rounds):
        if not subset:
            continue
        sweep = standard_policy_sweep(config, subset)
        # keep the table readable: singles, immediate races, queue cutovers,
        # and the best few fixed hedges
        singles = [x for x in sweep if x.name.startswith("single:")]
        races = [x for x in sweep if "@0s" in x.name]
        cutovers = sorted(
            (x for x in sweep if x.name.startswith("cutover:")),
            key=lambda x: (x.miss_rates.get(60, 1.0), x.mean_cost_usd),
        )[:3]
        hedges = sorted(
            (x for x in sweep if x.name.startswith("hedge:") and "@0s" not in x.name),
            key=lambda x: (x.miss_rates.get(60, 1.0), x.mean_cost_usd),
        )[:6]
        table = Table(
            title=f"Policy replay — {label} ({len(subset)} rounds)", header_style="bold"
        )
        for col in ("policy", "p50 s", "p95 s", "miss>60s (95% CI)",
                    "active $/req", "billed $/req", "hedge rate"):
            table.add_column(col, justify="right")
        for x in singles + races + hedges + cutovers:
            rec = x.to_record()
            lo, hi = x.miss_ci(60)
            table.add_row(
                rec["policy"],
                str(rec["p50_s"] if rec["p50_s"] is not None else "-"),
                str(rec["p95_s"] if rec["p95_s"] is not None else "-"),
                f"{x.miss_counts.get(60, 0)}/{x.n} ({lo:.0%}–{hi:.0%})",
                f"{x.mean_cost_usd:.4f}",
                f"{x.mean_billed_usd:.4f}",
                f"{x.hedge_rate:.0%}",
            )
        console.print(table)
        console.print(
            "[dim]active $ = execution-seconds x rate (loser idealized-cancelled); "
            "billed $ adds per-round idle windows; queued cancels assumed unbilled "
            "pending live validation.[/dim]"
        )

    # ------------------------------------------------------------------ costs
    if ledger_summary:
        console.print(
            f"[bold]Ledger:[/bold] projected ${ledger_summary['projected_total_usd']:.2f} "
            f"(stop ${ledger_summary['operational_stop_usd']:.0f}); "
            f"by stage {ledger_summary['by_stage']}"
        )
    if latest_snapshot:
        actual = latest_snapshot.get("actual_spend_since_baseline", {})
        console.print(f"[bold]Actual spend since baseline:[/bold] {actual}")


def _splits(config: BenchmarkConfig, rounds: list[Round]):
    """Full set, then the plan's calibration/evaluation split when applicable.

    The post-calibration rounds are the "evaluation set", not a strict holdout:
    every round was examined during the 2026-07 analysis. New policies get
    their own pre-registered validation runs (see benchmarks/)."""

    yield "all rounds", rounds
    calib_n = int(config.stages["moss"].get("calibration_rounds", 18))
    calib = [r for r in rounds if r.round_id <= calib_n]
    evaluation = [r for r in rounds if r.round_id > calib_n]
    if calib and evaluation:
        yield "calibration (policies frozen here)", calib
        yield "EVALUATION (rounds 19+; report these numbers)", evaluation


def latest_cost_snapshot(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    snaps = [r for r in read_traces(p) if r.get("kind") == "cost_snapshot"]
    return snaps[-1] if snaps else None
