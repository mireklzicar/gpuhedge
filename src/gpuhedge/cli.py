"""``gpuhedge`` command line — the operator entrypoint for the benchmark.

Subcommands:
  login-check   verify Modal / RunPod / Cerebrium auth (read-only, spends nothing)
  plan          print the $50 plan the config encodes (stages, gates, providers)
  config        show resolved rates / endpoints / budget
  costs         live provider-reported costs (balances/billing) vs projected ledger
  qualify       Stage 1 — deploy-gated Cerebrium qualification (max $4)
  bench         Stage 2 — three-provider MOSS cold-start dataset
  hedge         Stage 3 — a single live hedged request (primary -> hedge, cancel loser)

Nothing here submits a GPU job unless you ask for it: ``qualify``/``bench``/
``hedge`` require ``--go`` (or run with ``--dry-run`` to inspect the plan).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from gpuhedge import __version__
from gpuhedge.config import BenchmarkConfig, load_config

console = Console()


def _load(args: argparse.Namespace) -> BenchmarkConfig:
    return load_config(getattr(args, "config", None))


# --------------------------------------------------------------- login-check
def cmd_login_check(args: argparse.Namespace) -> int:
    from gpuhedge.auth import check_all

    console.print("[bold]Verifying provider logins[/bold] (live, read-only)…")
    statuses = check_all(timeout=args.timeout)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Identity")
    table.add_column("Detail")
    all_ok = True
    for s in statuses:
        all_ok = all_ok and s.logged_in
        colour = "green" if s.logged_in else "red"
        table.add_row(s.provider, f"[{colour}]{s.mark}[/{colour}]",
                      s.identity or "-", s.detail)
    console.print(table)
    if all_ok:
        console.print("[green]All three providers authenticated.[/green]")
    else:
        console.print("[red]Some providers are not logged in — see Detail above.[/red]")
    return 0 if all_ok else 1


# ---------------------------------------------------------------------- plan
def cmd_plan(args: argparse.Namespace) -> int:
    cfg = _load(args)
    console.print(f"[bold]GPUHedge $50 plan[/bold]  (model: {cfg.model})")
    console.print(f"config: {cfg.path}")

    prov = Table(title="Providers", header_style="bold")
    for col in ("key", "role", "gpu", "region", "$/s (all-in)", "deployed"):
        prov.add_column(col)
    for p in cfg.providers.values():
        prov.add_row(p.key, p.role, p.gpu, p.region, f"{p.billed_rate_per_s:.6f}",
                     "[green]yes[/green]" if p.deployed else "[yellow]no[/yellow]")
    console.print(prov)

    gates = Table(title="Budget gates (cumulative projected $)", header_style="bold")
    gates.add_column("gate")
    gates.add_column("limit $")
    for name, limit in cfg.budget.gates.items():
        gates.add_row(name, f"{limit:.0f}")
    gates.add_row("[dim]operational_stop[/dim]", f"[dim]{cfg.budget.operational_stop:.0f}[/dim]")
    gates.add_row("[dim]absolute_ceiling[/dim]", f"[dim]{cfg.budget.absolute_ceiling:.0f}[/dim]")
    console.print(gates)

    moss = cfg.stages["moss"]
    console.print(
        f"Stage 2 MOSS: {moss['blocks']}×{moss['rounds_per_block']} = "
        f"{moss['blocks'] * moss['rounds_per_block']} paired rounds "
        f"(calib {moss['calibration_rounds']} / eval {moss['evaluation_rounds']}), "
        f"cap {cfg.moss_timeout_s():.0f}s"
    )
    return 0


# -------------------------------------------------------------------- config
def cmd_config(args: argparse.Namespace) -> int:
    import json

    cfg = _load(args)
    console.print_json(json.dumps({
        "path": str(cfg.path),
        "model": cfg.model,
        "request": cfg.request,
        "policy": cfg.policy,
        "slo": cfg.slo,
        "timeouts_s": cfg.timeouts_s,
        "budget": {
            "gates": cfg.budget.gates,
            "operational_stop": cfg.budget.operational_stop,
            "absolute_ceiling": cfg.budget.absolute_ceiling,
        },
        "providers": {k: {"deployed": p.deployed, "gpu": p.gpu, "region": p.region,
                          "billed_rate_per_s": p.billed_rate_per_s, **p.extra}
                     for k, p in cfg.providers.items()},
    }))
    return 0


# --------------------------------------------------------------------- costs
def cmd_costs(args: argparse.Namespace) -> int:
    """Live provider-reported costs vs the projected ledger."""

    from gpuhedge.telemetry import CostLedger, CostMonitor

    cfg = _load(args)
    ledger = CostLedger(cfg)
    monitor = CostMonitor(cfg)

    table = Table(title="Provider-reported (live)", header_style="bold")
    for col in ("provider", "kind", "value $", "spent since baseline $", "detail"):
        table.add_column(col)
    record = monitor.snapshot(
        args.label or "manual", projected_total=ledger.projected_total
    )
    for provider, reading in record["readings"].items():
        spent = record["actual_spend_since_baseline"].get(provider)
        table.add_row(
            provider, reading["kind"],
            f"{reading['value_usd']:.4f}" if reading["value_usd"] is not None else "-",
            f"{spent:.4f}" if spent is not None else "-",
            reading["detail"][:70],
        )
    console.print(table)

    summary = ledger.summary()
    console.print(
        f"[bold]Ledger (projected):[/bold] ${summary['projected_total_usd']:.2f} "
        f"of ${summary['operational_stop_usd']:.0f} stop "
        f"(${summary['remaining_to_stop_usd']:.2f} headroom)"
    )
    if summary["by_stage"]:
        console.print(f"  by stage: {summary['by_stage']}")
    if summary["by_provider"]:
        console.print(f"  by provider: {summary['by_provider']}")
    if summary["reconciled"]:
        console.print(f"  reconciled (actual): {summary['reconciled']}")
    monitor.close()
    ledger.close()
    return 0


# -------------------------------------------------------------------- report
def cmd_report(args: argparse.Namespace) -> int:
    """Cold-start matrix, provider stats, policy replay, and cost summary."""

    from gpuhedge.benchmark.report import latest_cost_snapshot, print_report
    from gpuhedge.telemetry import CostLedger

    cfg = _load(args)
    rounds_path = args.traces or (cfg.trace_dir() / "moss_rounds.jsonl")
    ledger = CostLedger(cfg)
    print_report(
        cfg, console, rounds_path,
        ledger_summary=ledger.summary(),
        latest_snapshot=latest_cost_snapshot(cfg.trace_dir() / "costs.jsonl"),
    )
    ledger.close()
    return 0


# ---------------------------------------------------------------------- demo
def cmd_demo(args: argparse.Namespace) -> int:
    """No-cloud simulated race — no accounts, no credentials, no spend."""

    from gpuhedge.benchmark.demo import run_demo

    asyncio.run(run_demo(requests_per_policy=args.requests, console=console))
    return 0


# -------------------------------------------------------------------- replay
def cmd_replay(args: argparse.Namespace) -> int:
    """Reproduce the benchmark tables from a (committed) trace file."""

    from gpuhedge.benchmark.report import print_report

    cfg = _load(args)
    print_report(cfg, console, args.traces)
    return 0


# ------------------------------------------------------------------- qualify
def cmd_qualify(args: argparse.Namespace) -> int:
    from gpuhedge.benchmark.qualify import run_qualification

    cfg = _load(args)
    if not (args.go or args.dry_run):
        console.print("[yellow]Refusing to run without --go (or use --dry-run).[/yellow]")
        return 2
    report = asyncio.run(run_qualification(cfg, log=console.print, dry_run=args.dry_run))
    if not report.deployed:
        console.print(f"[yellow]{report.guidance}[/yellow]")
        return 3
    for c in report.criteria:
        colour = "green" if c.passed else "red"
        verdict = "PASS" if c.passed else "FAIL"
        console.print(f"  [{colour}]{verdict}[/{colour}] {c.name}: {c.detail}")
    return 0 if report.qualified else 1


# --------------------------------------------------------------------- bench
def cmd_bench(args: argparse.Namespace) -> int:
    from gpuhedge.benchmark.controller import run_moss_stage

    cfg = _load(args)
    if not (args.go or args.dry_run):
        console.print("[yellow]Refusing to spend money without --go (or use --dry-run).[/yellow]")
        return 2
    summary = asyncio.run(run_moss_stage(
        cfg, log=console.print, dry_run=args.dry_run,
        start_round=args.start_round, max_rounds=args.max_rounds,
        inter_round_wait_s=args.wait,
    ))
    console.print_json(data=summary)
    return 0


# --------------------------------------------------------------------- hedge
def cmd_hedge(args: argparse.Namespace) -> int:
    from gpuhedge.benchmark.live_hedge import run_hedged_request
    from gpuhedge.telemetry import CostLedger, TraceWriter

    cfg = _load(args)
    if not args.go:
        console.print("[yellow]Refusing to spend money without --go.[/yellow]")
        return 2
    ledger = CostLedger(cfg)
    trace = TraceWriter(cfg.trace_dir() / "live_hedge.jsonl")
    try:
        record = asyncio.run(run_hedged_request(
            cfg, ledger, trace,
            primary_key=args.primary, hedge_key=args.hedge,
            hedge_after_ms=args.hedge_after_ms,
        ))
    finally:
        trace.close()
        ledger.close()
    console.print_json(data=record)
    return 0


def cmd_hedge_stage(args: argparse.Namespace) -> int:
    from gpuhedge.benchmark.live_stage import run_live_hedging_stage

    cfg = _load(args)
    if not args.go:
        console.print("[yellow]Refusing to spend money without --go.[/yellow]")
        return 2
    summary = asyncio.run(run_live_hedging_stage(
        cfg, log=console.print,
        inter_request_wait_s=args.wait, start_request=args.start_request,
    ))
    console.print_json(data=summary)
    return 0


# ------------------------------------------------------------------- cutover
def cmd_cutover(args: argparse.Namespace) -> int:
    """One live queue-state-aware request (the PLAN_V2 policy)."""

    from gpuhedge.benchmark.state_aware import run_state_aware_request
    from gpuhedge.telemetry import CostLedger, TraceWriter

    cfg = _load(args)
    if not args.go:
        console.print("[yellow]Refusing to spend money without --go.[/yellow]")
        return 2
    ledger = CostLedger(cfg)
    trace = TraceWriter(cfg.trace_dir() / "state_aware.jsonl")
    try:
        record = asyncio.run(run_state_aware_request(
            cfg, ledger, trace,
            primary_key=args.primary, hedge_key=args.hedge,
            queue_cutover_ms=args.cutover_ms, safety_hedge_ms=args.safety_ms,
        ))
    finally:
        trace.close()
        ledger.close()
    record.pop("winner_metrics", None)
    console.print_json(data=record)
    return 0


# ------------------------------------------------------------------- cascade
def cmd_cascade(args: argparse.Namespace) -> int:
    """One live cascaded request (cutover -> safety hedge -> escalation)."""

    from gpuhedge.benchmark.cascade import run_cascade_request
    from gpuhedge.telemetry import CostLedger, TraceWriter

    cfg = _load(args)
    if not args.go:
        console.print("[yellow]Refusing to spend money without --go.[/yellow]")
        return 2
    ledger = CostLedger(cfg)
    trace = TraceWriter(cfg.trace_dir() / "cascade.jsonl")
    try:
        record = asyncio.run(run_cascade_request(
            cfg, ledger, trace,
            primary_key=args.primary, hedge_key=args.hedge,
            fallback_key=args.fallback,
            queue_cutover_ms=args.cutover_ms, safety_hedge_ms=args.safety_ms,
            escalate_after_ms=args.escalate_ms,
        ))
    finally:
        trace.close()
        ledger.close()
    record.pop("winner_metrics", None)
    console.print_json(data=record)
    return 0


# -------------------------------------------------------------- cancel-audit
def cmd_cancel_audit(args: argparse.Namespace) -> int:
    from gpuhedge.benchmark.cancel_audit import run_cancel_audit

    cfg = _load(args)
    if not args.go:
        console.print("[yellow]Refusing to spend money without --go.[/yellow]")
        return 2
    summary = asyncio.run(run_cancel_audit(
        cfg, providers=args.providers.split(",") if args.providers else None,
        repeats=args.repeats, inter_job_wait_s=args.wait, log=console.print,
    ))
    console.print_json(data=summary)
    return 0


# ------------------------------------------------------------------ validate
def cmd_validate(args: argparse.Namespace) -> int:
    from gpuhedge.benchmark.validation import block_order, load_prereg, run_validation_stage

    cfg = _load(args)
    if args.dry_run:
        prereg = load_prereg(args.prereg)
        console.print(f"experiment: {prereg['experiment']}")
        console.print(f"block order (seed {prereg['design']['block_order_seed']}): "
                      f"{block_order(prereg)}")
        return 0
    if not args.go:
        console.print("[yellow]Refusing to spend money without --go (or use --dry-run).[/yellow]")
        return 2
    summary = asyncio.run(run_validation_stage(
        cfg, args.prereg, log=console.print, start_block=args.start_block,
    ))
    console.print_json(data=summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpuhedge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"gpuhedge {__version__}")
    parser.add_argument("--config", help="path to benchmark.yaml (default: packaged/local)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login-check", help="verify Modal/RunPod/Cerebrium auth")
    p.add_argument("--timeout", type=float, default=45.0)
    p.set_defaults(func=cmd_login_check)

    sub.add_parser("plan", help="print the $50 plan").set_defaults(func=cmd_plan)
    sub.add_parser("config", help="show resolved config").set_defaults(func=cmd_config)

    p = sub.add_parser("demo", help="no-cloud simulated race (no accounts, no spend)")
    p.add_argument("--requests", type=int, default=8, help="requests per policy")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("replay", help="reproduce benchmark tables from a trace file")
    p.add_argument("traces", help="path to a rounds .jsonl (e.g. the committed traces)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("costs", help="live provider-reported costs vs projected ledger")
    p.add_argument("--label", default=None, help="label for the snapshot record")
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("report", help="cold-start matrix + policy replay from traces")
    p.add_argument("--traces", default=None, help="path to moss_rounds.jsonl")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("qualify", help="Stage 1 — Cerebrium qualification (max $4)")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_qualify)

    p = sub.add_parser("bench", help="Stage 2 — MOSS cold-start dataset")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--start-round", type=int, default=1)
    p.add_argument("--max-rounds", type=int, default=None)
    p.add_argument("--wait", type=float, default=130.0, help="inter-round scale-to-zero wait (s)")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("hedge", help="Stage 3 — one live hedged request")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--primary", default="runpod")
    p.add_argument("--hedge", default="modal")
    p.add_argument("--hedge-after-ms", type=int, default=None)
    p.set_defaults(func=cmd_hedge)

    p = sub.add_parser("hedge-stage", help="Stage 3 — the full 18-request plan")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--start-request", type=int, default=1)
    p.add_argument("--wait", type=float, default=130.0)
    p.set_defaults(func=cmd_hedge_stage)

    p = sub.add_parser("cutover", help="one live queue-state-aware request")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--primary", default="runpod")
    p.add_argument("--hedge", default="cerebrium")
    p.add_argument("--cutover-ms", type=int, default=2500)
    p.add_argument("--safety-ms", type=int, default=8500)
    p.set_defaults(func=cmd_cutover)

    p = sub.add_parser("cascade", help="one live cascaded request (three providers)")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--primary", default="runpod")
    p.add_argument("--hedge", default="cerebrium")
    p.add_argument("--fallback", default="modal")
    p.add_argument("--cutover-ms", type=int, default=2500)
    p.add_argument("--safety-ms", type=int, default=8500)
    p.add_argument("--escalate-ms", type=int, default=25000)
    p.set_defaults(func=cmd_cascade)

    p = sub.add_parser("cancel-audit", help="forced loser-cancel matrix (all providers)")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--providers", default=None, help="comma list (default: all)")
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--wait", type=float, default=130.0)
    p.set_defaults(func=cmd_cancel_audit)

    p = sub.add_parser("validate", help="run a pre-registered validation experiment")
    p.add_argument("--go", action="store_true", help="actually run (spends money)")
    p.add_argument("--dry-run", action="store_true", help="print the block order only")
    p.add_argument("--prereg",
                   default="benchmarks/2026-07-queue-cutover/preregistration.yaml")
    p.add_argument("--start-block", type=int, default=1)
    p.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
