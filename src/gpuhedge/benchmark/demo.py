"""``gpuhedge demo`` — the no-cloud, no-signup policy demo.

Races simulated providers (backends/sim_backend.py) through the REAL policy
engines: single-provider, fixed delayed hedge (live_hedge.py), the
queue-state cutover (state_aware.py), and the cascaded hedge (cascade.py).
Prints a per-request timeline and a summary table. Times shown are simulated
seconds (wall time is compressed by the config's ``sim.time_scale``).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from gpuhedge.backends import build_backend
from gpuhedge.benchmark.cascade import run_cascade_request
from gpuhedge.benchmark.live_hedge import run_hedged_request
from gpuhedge.benchmark.state_aware import run_state_aware_request
from gpuhedge.config import BenchmarkConfig, load_config
from gpuhedge.telemetry import CostLedger, TraceWriter
from gpuhedge.validators import validate_wav

DEMO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "demo.yaml"


def _scale(config: BenchmarkConfig) -> float:
    primary = config.provider(config.policy["primary"])
    return float(primary.extra.get("sim", {}).get("time_scale", 0.05))


async def _single(config, ledger, trace, *, provider_key, timeout_s, request_id):
    backend = build_backend(config.provider(provider_key), config.request)
    handle = await backend.submit()
    result = await handle.result(timeout_s)
    valid = validate_wav(result.audio).valid
    return {
        "policy": f"single:{provider_key}",
        "winner": provider_key if valid else None,
        "winner_total_ms": round(result.wall_s * 1000, 1) if valid else None,
        "cutover_fired": False,
        "cancellation": None,
        "winner_metrics": result.provider_metrics,
    }


async def run_demo(requests_per_policy: int = 8, *,
                   console: Console | None = None) -> dict[str, Any]:
    from gpuhedge.backends.sim_backend import reset_sim_streams

    console = console or Console()
    reset_sim_streams()
    config = load_config(DEMO_CONFIG)
    scale = _scale(config)
    primary = config.policy["primary"]
    hedge = config.policy["hedge_choose_one_of"][0]
    fallback = config.policy.get("fallback")
    cap_sim_s = config.moss_timeout_s()
    cap_real_s = cap_sim_s * scale

    tmp = tempfile.mkdtemp(prefix="gpuhedge-demo-")
    ledger_dir = Path(tmp)
    trace = TraceWriter(ledger_dir / "demo.jsonl")
    import os

    os.environ["GPUHEDGE_TRACE_DIR"] = str(ledger_dir)
    ledger = CostLedger(config)

    console.print(f"[bold]GPUHedge demo[/bold] — simulated providers, real policy "
                  f"engines ({1 / scale:.0f}x compressed time). Traces: {tmp}")
    console.print(
        f"  [cyan]{primary}[/cyan]: usually ~6 s, sometimes ~90-120 s behind a "
        f"long queue delay\n  [magenta]{hedge}[/magenta]: steady ~16-20 s, "
        f"rare ~85-105 s tail"
        + (f"\n  [green]{fallback}[/green]: predictable ~26-32 s escalation "
           f"target\n" if fallback else "\n"))

    summary: dict[str, list[dict[str, Any]]] = {}
    policies = [
        ("single", f"single:{primary}"),
        ("fixed-hedge@10s", f"hedge {primary}->{hedge} after 10 s"),
        ("queue-cutover@2.5s", f"poll {primary} state at 2.5 s; cancel if queued"),
    ]
    if fallback:
        policies.append(
            ("cascade@25s", f"cutover + escalate to {fallback} at 25 s if the "
                            f"hedge stalls"))
    try:
        for policy_key, blurb in policies:
            reset_sim_streams()  # every policy faces the SAME draw sequence
            console.print(f"[bold underline]{policy_key}[/bold underline] — {blurb}")
            records = []
            for i in range(1, requests_per_policy + 1):
                if policy_key == "single":
                    rec = await _single(config, ledger, trace,
                                        provider_key=primary,
                                        timeout_s=cap_real_s, request_id=i)
                elif policy_key == "fixed-hedge@10s":
                    rec = await run_hedged_request(
                        config, ledger, trace, primary_key=primary,
                        hedge_key=hedge,
                        hedge_after_ms=int(10_000 * scale),
                        timeout_s=cap_real_s, request_id=i)
                elif policy_key == "queue-cutover@2.5s":
                    rec = await run_state_aware_request(
                        config, ledger, trace, primary_key=primary,
                        hedge_key=hedge,
                        queue_cutover_ms=int(2_500 * scale),
                        safety_hedge_ms=int(8_500 * scale),
                        timeout_s=cap_real_s, request_id=i)
                else:
                    rec = await run_cascade_request(
                        config, ledger, trace, primary_key=primary,
                        hedge_key=hedge, fallback_key=fallback,
                        queue_cutover_ms=int(2_500 * scale),
                        safety_hedge_ms=int(8_500 * scale),
                        escalate_after_ms=int(25_000 * scale),
                        timeout_s=cap_real_s, request_id=i)
                records.append(rec)
                console.print("  " + _timeline(rec, scale, primary, hedge))
            summary[policy_key] = records
            console.print()
    finally:
        trace.close()
        ledger.close()

    _print_summary(console, summary, config, scale, primary, hedge)
    return {k: len(v) for k, v in summary.items()}


def _timeline(rec: dict[str, Any], scale: float, primary: str, hedge: str) -> str:
    total = rec.get("winner_total_ms")
    total_s = (total / 1000.0 / scale) if total is not None else None
    winner = rec.get("winner")
    parts = []
    if rec.get("cutover_fired"):
        parts.append(f"[cyan]{primary}[/cyan] still queued at poll → "
                     f"[red]cancelled before its worker started[/red] → "
                     f"[magenta]{hedge}[/magenta] takes over")
    elif rec.get("safety_hedge_fired") or rec.get("hedge_launched"):
        parts.append(f"[cyan]{primary}[/cyan] slow → "
                     f"[magenta]{hedge}[/magenta] hedge launched")
        c = rec.get("cancellation")
        if c:
            parts.append(f"loser [red]{c['provider']}[/red] cancelled")
        elif rec.get("winner") == hedge:
            parts.append(f"[cyan]{primary}[/cyan] returned "
                         f"[red]invalid audio[/red] — validator rejected it")
    elif total_s is not None and total_s > 30:
        parts.append(f"[cyan]{primary}[/cyan] [orange3]slow path "
                     f"(fresh worker)[/orange3]")
    else:
        parts.append(f"[cyan]{primary}[/cyan] fast path")
    if rec.get("escalation_fired"):
        parts.append(f"[magenta]{hedge}[/magenta] stalled → "
                     f"[green]escalated to the fallback[/green]")
    if winner is None:
        parts.append("[red]no valid result[/red]")
    else:
        colour = ("cyan" if winner == primary
                  else "magenta" if winner == hedge else "green")
        parts.append(f"[{colour}]{winner}[/{colour}] valid at "
                     f"[bold]{total_s:.1f}s[/bold]")
    return " | ".join(parts)


def _print_summary(console, summary, config, scale, primary, hedge) -> None:
    table = Table(title="Demo summary (simulated seconds)", header_style="bold")
    for col in ("policy", "n", "p50 s", "p95 s", "miss>60s", "hedged/switched",
                "mean sim $/req"):
        table.add_column(col, justify="right")
    rates = {k: p.billed_rate_per_s for k, p in config.providers.items()}
    c_h = rates[hedge]
    for key, records in summary.items():
        lats = []
        costs = []
        hedged = 0
        for r in records:
            t = r.get("winner_total_ms")
            lat = (t / 1000.0 / scale) if t is not None else math.inf
            lats.append(lat)
            fired = bool(r.get("cutover_fired") or r.get("safety_hedge_fired")
                         or r.get("hedge_launched") or r.get("escalation_fired"))
            hedged += int(fired)
            winner = r.get("winner")
            win_s = 0.0 if math.isinf(lat) else lat
            receipts = (r.get("cancellations")
                        or ([r["cancellation"]] if r.get("cancellation") else []))
            if winner in rates and winner != primary:
                cost = rates[winner] * win_s
                cost += sum(float(c.get("estimated_cost_usd") or 0.0)
                            for c in receipts)
            else:
                cost = rates[primary] * win_s + (c_h * 10.0 if fired else 0.0)
            costs.append(cost)
        finite = sorted(x for x in lats if not math.isinf(x))

        def pct(q: float, xs: list[float] = finite) -> float:
            if not xs:
                return math.inf
            return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]

        n = len(records)
        table.add_row(
            key, str(n), f"{pct(0.5):.1f}", f"{pct(0.95):.1f}",
            f"{sum(1 for x in lats if x > 60)}/{n}", f"{hedged}/{n}",
            f"{sum(costs) / n:.4f}",
        )
    console.print(table)
    console.print("[dim]Same engines, simulated latencies — run "
                  "`gpuhedge replay <traces.jsonl>` on the committed benchmark "
                  "traces to reproduce the real numbers.[/dim]")
