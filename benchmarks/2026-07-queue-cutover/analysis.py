"""Analyze the pre-registered queue-cutover validation run.

    python benchmarks/2026-07-queue-cutover/analysis.py [--traces traces/]

Reads traces/validation.jsonl (+ costs.jsonl for per-block balance deltas)
and reports exactly what preregistration.yaml §reporting promises: per-arm
latency stats with Wilson intervals, switch rates, receipts, and both cost
layers. Also prints a verdict per registered hypothesis.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from gpuhedge.benchmark.replay import wilson_interval

INF = math.inf
DEADLINES = (30, 60, 90, 120)
RUNPOD_RATE = 0.000306
CEREBRIUM_RATE = 0.000736
VOLUME_USD_PER_HR = 0.011   # two network volumes billing during the run


def read_jsonl(path: Path):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _pct(xs: list[float], q: float) -> float:
    """Nearest-rank order statistic — the estimator the original report used."""

    xs = sorted(xs)
    if not xs:
        return INF
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def _pct_linear(xs: list[float], q: float) -> float:
    """Linear interpolation (numpy's default) — reported alongside because at
    n=20 the two estimators disagree materially on tails, and the
    preregistration did not fix one (review finding, 2026-07-12)."""

    xs = sorted(xs)
    if not xs:
        return INF
    h = q * (len(xs) - 1)
    lo = int(math.floor(h))
    hi = min(lo + 1, len(xs) - 1)
    if math.isinf(xs[lo]) or math.isinf(xs[hi]):
        return INF
    return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


def load_validation(trace_dir: Path):
    """Join index records (arm/block) with the full engine records."""

    full: dict[int, dict] = {}
    index: list[dict] = []
    for rec in read_jsonl(trace_dir / "validation.jsonl"):
        kind = rec.get("kind")
        if kind in ("single_request", "hedged_request", "state_aware_request"):
            full[rec["request_id"]] = rec
        elif kind == "validation_index":
            index.append(rec)
    rows = []
    for idx in index:
        rec = full.get(idx["request_id"], {})
        rows.append({**rec, **idx})
    return rows


def arm_stats(rows: list[dict]) -> dict:
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
    out = {}
    for arm, rs in by_arm.items():
        lats = [(r["winner_total_ms"] / 1000.0) if r.get("winner_total_ms")
                else INF for r in rs]
        finite = [x for x in lats if not math.isinf(x)]
        hedged = sum(1 for r in rs if r.get("hedge_launched")
                     or r.get("cutover_fired") or r.get("safety_hedge_fired"))
        cancels = [r["cancellation"] for r in rs if r.get("cancellation")]
        out[arm] = {
            "n": len(rs),
            "valid": len(finite),
            "p50_s": round(_pct(lats, 0.5), 1),
            "p90_s": round(_pct(lats, 0.90), 1),
            "p95_s": round(_pct(lats, 0.95), 1),
            "p95_linear_s": round(_pct_linear(lats, 0.95), 1),
            "mean_finite_s": round(sum(finite) / len(finite), 1) if finite else None,
            "max_s": round(max(finite), 1) if finite else None,
            "miss_counts": {d: sum(1 for x in lats if x > d) for d in DEADLINES},
            "hedged": hedged,
            "cutovers": sum(1 for r in rs if r.get("cutover_fired")),
            "safety_hedges": sum(1 for r in rs if r.get("safety_hedge_fired")),
            "cancel_acks_ms": sorted(
                round(c["cancel_ack_ms"] - c["cancel_sent_ms"], 0)
                for c in cancels if c.get("cancel_ack_ms")),
            "cancel_leaked": sum(1 for c in cancels if c.get("leaked")),
            "queued_cancels_with_zero_exec": sum(
                1 for c in cancels
                if not c.get("was_running")
                and c.get("execution_ms_before_cancel") == 0
                and c.get("reconciled_cost_usd") == 0),
            "queued_cancels_legacy_unreconciled": sum(
                1 for c in cancels
                if not c.get("was_running")
                and c.get("reconciled_cost_usd") is None),
        }
    return out


def billing_deltas(trace_dir: Path):
    """Per-block RunPod balance deltas from the val-bNN-arm-start/end snapshots."""

    snaps = [r for r in read_jsonl(trace_dir / "costs.jsonl")
             if r.get("kind") == "cost_snapshot"
             and str(r.get("label", "")).startswith("val-b")]
    by_label = {}
    for s in snaps:
        bal = (s.get("readings", {}).get("runpod", {}) or {}).get("value_usd")
        spent = (s.get("actual_spend_since_baseline", {}) or {}).get("runpod")
        by_label[s["label"]] = {
            "balance": bal,
            "spent": spent,
            "ts": s.get("ts"),
        }
    blocks = defaultdict(dict)
    for label, v in by_label.items():
        # val-b03-single-runpod-start
        parts = label.split("-")
        b = int(parts[1][1:])
        arm = "-".join(parts[2:-1])
        blocks[(b, arm)][parts[-1]] = v
    out = []
    for (b, arm), se in sorted(blocks.items()):
        start, end = se.get("start"), se.get("end")
        if not (start and end):
            continue
        t0 = datetime.fromisoformat(start["ts"])
        t1 = datetime.fromisoformat(end["ts"])
        hours = max(0.0, (t1 - t0).total_seconds() / 3600.0)
        if start["balance"] is not None and end["balance"] is not None:
            raw = start["balance"] - end["balance"]
        elif start["spent"] is not None and end["spent"] is not None:
            # Sanitized traces withhold absolute balances but retain the
            # spend-since-baseline series, whose block differences are equal.
            raw = end["spent"] - start["spent"]
        else:
            continue
        adj = raw - VOLUME_USD_PER_HR * hours
        out.append({"block": b, "arm": arm, "raw_delta_usd": round(raw, 4),
                    "storage_adj_usd": round(VOLUME_USD_PER_HR * hours, 4),
                    "runpod_billed_usd": round(adj, 4)})
    return out


def modeled_active_cost(rows: list[dict]) -> dict[str, float]:
    """Provider-reported active-compute $/req from the engine records.

    Winner wall time includes queueing, so multiplying it by the GPU rate is
    not an active-compute model. Prefer each provider's billed execution
    metric, falling back to execution seconds only for older records.
    """

    by_arm: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        arm = r["arm"]
        winner = r.get("winner")
        metrics = r.get("winner_metrics") or {}
        cost = 0.0
        if winner == "runpod":
            reported = metrics.get("runpod_billed_cost_usd")
            if reported is not None:
                cost += float(reported)
            else:
                cost += RUNPOD_RATE * float(metrics.get("runpod_execution_ms") or 0) / 1000
        elif winner == "cerebrium":
            reported = metrics.get("cerebrium_billed_cost_usd")
            if reported is not None:
                cost += float(reported)
            elif metrics.get("cerebrium_run_time_ms") is not None:
                cost += CEREBRIUM_RATE * float(metrics["cerebrium_run_time_ms"]) / 1000
            else:
                # Hedge engine traces written by the validation run predate
                # winner_metrics capture. winner_valid_at_ms is provider-side
                # elapsed time from hedge launch in those records.
                cost += CEREBRIUM_RATE * float(r.get("winner_valid_at_ms") or 0) / 1000
        c = r.get("cancellation") or {}
        if c:
            reconciled = c.get("reconciled_cost_usd")
            if reconciled is not None:
                cost += float(reconciled)
            elif c.get("was_running"):
                cost += float(c.get("estimated_cost_usd") or 0.0)
            # else: cancelled while queued -> no worker, no execution cost.
            # (Records written before the adapter fix carry a wall-clock
            # estimated_cost_usd for queued cancels; ignore it.)
        by_arm[arm].append(cost)
    return {arm: round(sum(v) / len(v), 5) for arm, v in by_arm.items() if v}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", default="traces")
    args = parser.parse_args()
    trace_dir = Path(args.traces)

    rows = load_validation(trace_dir)
    print(f"{len(rows)} validation requests\n")

    stats = arm_stats(rows)
    print(f"{'arm':18s} {'n':>3s} {'p50':>6s} {'p90':>7s} {'p95nr':>7s} "
          f"{'p95li':>7s} {'max':>7s} {'miss>60 (CI)':>16s} {'hedged':>7s} "
          f"{'cutover':>8s}")
    for arm in ("single-runpod", "fixed-hedge-10s", "queue-cutover"):
        s = stats.get(arm)
        if not s:
            continue
        k = s["miss_counts"][60]
        lo, hi = wilson_interval(k, s["n"])
        print(f"{arm:18s} {s['n']:3d} {s['p50_s']:6.1f} {s['p90_s']:7.1f} "
              f"{s['p95_s']:7.1f} {s['p95_linear_s']:7.1f} "
              f"{s['max_s'] if s['max_s'] is not None else float('nan'):7.1f} "
              f"{k}/{s['n']} ({lo:.0%}–{hi:.0%})".ljust(84)
              + f" {s['hedged']:>3d}/{s['n']:<3d} "
              f"{s['cutovers']:>3d}+{s['safety_hedges']}s")
    print("\n(p95nr = nearest-rank as originally reported; p95li = linear "
          "interpolation, numpy default.\n At n=20 the tail estimate is set "
          "by 1-2 requests: prefer p50/p90/max/miss counts.)\n")

    active = modeled_active_cost(rows)
    print("modeled active-compute $/req:", active, "\n")

    deltas = billing_deltas(trace_dir)
    per_arm = defaultdict(list)
    for d in deltas:
        per_arm[d["arm"]].append(d["runpod_billed_usd"])
        print(f"  block {d['block']:2d} {d['arm']:16s} runpod balance delta "
              f"${d['raw_delta_usd']:.4f} (−${d['storage_adj_usd']:.4f} storage)"
              f" → ${d['runpod_billed_usd']:.4f}")
    print("\nper-arm RunPod billed (balance deltas, storage-adjusted):")
    arm_billed = {}
    for arm, vs in per_arm.items():
        n_req = sum(1 for r in rows if r["arm"] == arm)
        arm_billed[arm] = sum(vs)
        print(f"  {arm:18s} total ${sum(vs):.4f} over {len(vs)} blocks "
              f"→ ${sum(vs) / max(1, n_req):.5f}/req (runpod side only)")

    # ---------------- hypotheses ----------------
    print("\nregistered hypotheses:")
    s_cut = stats.get("queue-cutover")
    s_fix = stats.get("fixed-hedge-10s")
    if s_cut and s_fix:
        # The preregistration did not fix the quantile estimator, and at n=20
        # the two standard choices disagree on which arm wins. Reporting the
        # verdict as estimator-sensitive rather than claiming a pass.
        nr = s_cut["p95_s"] <= s_fix["p95_s"]
        li = s_cut["p95_linear_s"] <= s_fix["p95_linear_s"]
        verdict = ("PASS" if nr and li
                   else "FAIL" if not nr and not li
                   else "ESTIMATOR-SENSITIVE (inconclusive)")
        print(f"  H1 latency: cutover p95 vs fixed p95 — nearest-rank "
              f"{s_cut['p95_s']} vs {s_fix['p95_s']} ({'≤' if nr else '>'}), "
              f"linear {s_cut['p95_linear_s']} vs {s_fix['p95_linear_s']} "
              f"({'≤' if li else '>'}) → {verdict}")
    if s_cut:
        k = s_cut["miss_counts"][60]
        print(f"  H2 misses: cutover 60s misses {k}/{s_cut['n']} → "
              f"{'PASS' if k == 0 else 'FAIL'}")
    if all(a in active for a in ("queue-cutover", "fixed-hedge-10s", "single-runpod")):
        ok = (active["queue-cutover"] < active["fixed-hedge-10s"]
              < active["single-runpod"])
        print(f"  H3 active cost order: {active['queue-cutover']} < "
              f"{active['fixed-hedge-10s']} < {active['single-runpod']} → "
              f"{'PASS' if ok else 'FAIL'}")
    if {"queue-cutover", "single-runpod"} <= set(per_arm):
        n_cut = sum(1 for r in rows if r["arm"] == "queue-cutover")
        n_run = sum(1 for r in rows if r["arm"] == "single-runpod")
        cut_pr = arm_billed["queue-cutover"] / max(1, n_cut)
        run_pr = arm_billed["single-runpod"] / max(1, n_run)
        print(f"  H4 billed (runpod side): cutover ${cut_pr:.5f}/req < "
              f"single ${run_pr:.5f}/req → {'PASS' if cut_pr < run_pr else 'FAIL'}")
    if s_cut:
        z = s_cut["queued_cancels_with_zero_exec"]
        legacy = s_cut["queued_cancels_legacy_unreconciled"]
        print(f"  H5 queued cancels report no billed execution: "
              f"{z}/{s_cut['cutovers'] - legacy} post-fix cutover receipts; "
              f"{legacy} legacy receipts unreconciled → "
              f"{'PASS' if z == s_cut['cutovers'] - legacy else 'CHECK RECEIPTS'}")


if __name__ == "__main__":
    main()
