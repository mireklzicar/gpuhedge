"""Offline policy replay over paired cold-start traces (benchmarks/2026-07-moss/methodology.md).

Hundreds of candidate policies are evaluated from the same paired rounds
without spending more GPU money:

    L_i(d) = R_i                     if R_i <= d          (primary fast path)
           = min(R_i, d + M_i)       otherwise            (hedge launched at d)

    C_i(d) = c_R * min(R_i, L_i(d)) + c_M * max(0, L_i(d) - d)

Censored/invalid results are treated as +inf latency (they billed the full cap).
Policies: single provider, immediate pair races (d=0), fixed delayed hedges,
and the queue-state cutover (cancel the primary while still queued).

Two cost models are reported for every policy:

- ``mean_cost_usd`` — modeled ACTIVE-COMPUTE cost: execution seconds x rate,
  loser idealized as cancelled at winner time. This is what the replay math
  above computes and is comparable across policies, but it is NOT the cloud
  bill.
- ``mean_billed_usd`` — modeled BILLED cost: adds each provider's configured
  ``idle_billed_seconds`` for every round in which that provider's worker
  actually started (RunPod bills the post-job idle window; docs.runpod.io
  /serverless/pricing). A job cancelled while still IN_QUEUE is assumed to
  bill nothing — an assumption the pre-registered live validation must
  confirm against account balance deltas before it is publishable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry.trace import read_traces

INF = math.inf


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion (k successes of n)."""

    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Round:
    round_id: int
    block: int
    # provider -> (latency_s or INF, billed_seconds_for_cost)
    latency: dict[str, float]
    billed: dict[str, float]
    # provider -> queue delay in seconds (present where the provider reports
    # it; RunPod exposes delayTime on every job)
    queue_delay_s: dict[str, float] = field(default_factory=dict)


def load_rounds(path: str | Path) -> list[Round]:
    rounds = []
    for rec in read_traces(path):
        if rec.get("kind") != "round":
            continue
        latency: dict[str, float] = {}
        billed: dict[str, float] = {}
        queue_delay: dict[str, float] = {}
        for key, p in rec.get("providers", {}).items():
            wall = float(p.get("wall_s", INF))
            ok = bool(p.get("valid"))
            latency[key] = wall if ok else INF
            billed[key] = wall  # capped by the controller; billed even if invalid
            delay_ms = (p.get("metrics") or {}).get(f"{key}_delay_ms")
            if delay_ms is not None:
                queue_delay[key] = float(delay_ms) / 1000.0
        rounds.append(
            Round(rec["round_id"], rec.get("block", 0), latency, billed, queue_delay)
        )
    return rounds


def _pct(values: list[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        return INF
    idx = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[idx]


@dataclass
class PolicyResult:
    name: str
    n: int
    p50_s: float
    p95_s: float
    max_s: float
    miss_rates: dict[int, float]          # deadline_s -> fraction of misses
    miss_counts: dict[int, int]           # deadline_s -> number of misses
    mean_cost_usd: float                  # modeled active-compute $/req
    mean_billed_usd: float                # modeled billed $/req (incl. idle windows)
    hedge_rate: float                     # fraction of rounds hedge launched

    def miss_ci(self, deadline_s: int) -> tuple[float, float]:
        return wilson_interval(self.miss_counts.get(deadline_s, 0), self.n)

    def to_record(self) -> dict[str, Any]:
        return {
            "policy": self.name, "n": self.n,
            "p50_s": None if math.isinf(self.p50_s) else round(self.p50_s, 1),
            "p95_s": None if math.isinf(self.p95_s) else round(self.p95_s, 1),
            "max_s": None if math.isinf(self.max_s) else round(self.max_s, 1),
            "miss_rates": {k: round(v, 3) for k, v in self.miss_rates.items()},
            "miss_counts": dict(self.miss_counts),
            "miss_ci95": {
                k: [round(x, 3) for x in self.miss_ci(k)] for k in self.miss_counts
            },
            "mean_cost_usd": round(self.mean_cost_usd, 4),
            "mean_billed_usd": round(self.mean_billed_usd, 4),
            "hedge_rate": round(self.hedge_rate, 3),
        }


def evaluate_single(
    config: BenchmarkConfig, rounds: list[Round], provider: str,
    deadlines: list[int],
) -> PolicyResult:
    p = config.provider(provider)
    lat = [r.latency.get(provider, INF) for r in rounds]
    cost = [r.billed.get(provider, 0.0) * p.billed_rate_per_s for r in rounds]
    # single provider: the worker starts every round -> idle billed every round
    billed = [c + p.idle_billed_seconds * p.billed_rate_per_s for c in cost]
    return _summarize(f"single:{provider}", lat, cost, billed, deadlines,
                      hedges=0, n=len(rounds))


def evaluate_hedge(
    config: BenchmarkConfig, rounds: list[Round], primary: str, hedge: str,
    delay_s: float, deadlines: list[int], cap_s: float = 300.0,
) -> PolicyResult:
    """Fixed delayed hedge replay; delay_s=0 is the immediate pair race."""

    p_prov = config.provider(primary)
    h_prov = config.provider(hedge)
    c_p, c_h = p_prov.billed_rate_per_s, h_prov.billed_rate_per_s
    idle_p = p_prov.idle_billed_seconds * c_p
    idle_h = h_prov.idle_billed_seconds * c_h
    lats: list[float] = []
    costs: list[float] = []
    billeds: list[float] = []
    hedges = 0
    for r in rounds:
        rp = r.latency.get(primary, INF)
        rh = r.latency.get(hedge, INF)
        q_p = r.queue_delay_s.get(primary)
        if rp <= delay_s:
            latency = rp
            cost = c_p * min(r.billed.get(primary, cap_s), rp)
            billed = cost + idle_p
        else:
            hedges += 1
            latency = min(rp, delay_s + rh)
            if math.isinf(latency):
                latency = INF
                # both censored: pay both to their caps
                cost = c_p * r.billed.get(primary, cap_s) + c_h * r.billed.get(hedge, cap_s)
                billed = cost + idle_p + idle_h
            else:
                # winner billed to finish; loser cancelled at winner time
                # (idealized: Stage 3 measured ~0.3-0.4 s of extra loser runtime)
                primary_started = q_p is None or latency > q_p
                if primary_started:
                    cost = c_p * min(r.billed.get(primary, cap_s), latency)
                    billed_p_part = cost + idle_p
                else:
                    # cancelled while still queued: assumed unbilled (validate live)
                    cost = 0.0
                    billed_p_part = 0.0
                hedge_part = c_h * max(0.0, latency - delay_s)
                cost += hedge_part
                billed = billed_p_part + hedge_part + idle_h
        lats.append(latency)
        costs.append(cost)
        billeds.append(billed)
    return _summarize(
        f"hedge:{primary}->{hedge}@{delay_s:g}s", lats, costs, billeds, deadlines,
        hedges=hedges, n=len(rounds),
    )


def evaluate_queue_cutover(
    config: BenchmarkConfig, rounds: list[Round], primary: str, hedge: str,
    deadlines: list[int], *, cutover_s: float = 2.5, safety_s: float = 8.5,
    cap_s: float = 300.0,
) -> PolicyResult:
    """Queue-state cutover replay (docs/policies.md).

    At ``cutover_s`` the primary's job state is polled. Still IN_QUEUE ->
    cancel it before a worker starts and launch the hedge. IN_PROGRESS ->
    keep it, but launch a safety hedge at ``safety_s`` if no valid result yet.

    Replay approximation: the primary is "still queued at t" iff its reported
    queue delay exceeds t. A queued cancel is assumed to bill nothing (no
    worker, no idle window) — the headline assumption the pre-registered live
    validation exists to test. Rounds without a queue-delay signal for the
    primary are replayed as a plain fixed hedge at ``safety_s``.
    """

    p_prov = config.provider(primary)
    h_prov = config.provider(hedge)
    c_p, c_h = p_prov.billed_rate_per_s, h_prov.billed_rate_per_s
    idle_p = p_prov.idle_billed_seconds * c_p
    idle_h = h_prov.idle_billed_seconds * c_h
    lats: list[float] = []
    costs: list[float] = []
    billeds: list[float] = []
    switches = 0
    for r in rounds:
        rp = r.latency.get(primary, INF)
        rh = r.latency.get(hedge, INF)
        q_p = r.queue_delay_s.get(primary, INF if math.isinf(rp) else 0.0)
        if q_p > cutover_s:
            # still queued at the poll -> cancel unstarted primary, go hedge
            switches += 1
            latency = cutover_s + rh
            cost = c_h * (r.billed.get(hedge, cap_s) if math.isinf(rh) else rh)
            billed = cost + idle_h
        elif rp <= safety_s:
            # FlashBoot-path primary finished before the safety hedge
            latency = rp
            cost = c_p * min(r.billed.get(primary, cap_s), rp)
            billed = cost + idle_p
        else:
            # running but slow -> safety hedge at safety_s, first valid wins
            switches += 1
            latency = min(rp, safety_s + rh)
            if math.isinf(latency):
                cost = c_p * r.billed.get(primary, cap_s) + c_h * r.billed.get(hedge, cap_s)
                billed = cost + idle_p + idle_h
            else:
                cost = c_p * min(r.billed.get(primary, cap_s), latency)
                cost += c_h * max(0.0, latency - safety_s)
                billed = cost + idle_p + idle_h
        lats.append(latency)
        costs.append(cost)
        billeds.append(billed)
    return _summarize(
        f"cutover:{primary}->{hedge}@q{cutover_s:g}s+s{safety_s:g}s",
        lats, costs, billeds, deadlines, hedges=switches, n=len(rounds),
    )


def evaluate_cascade(
    config: BenchmarkConfig, rounds: list[Round], primary: str, hedge: str,
    fallback: str, deadlines: list[int], *, cutover_s: float = 2.5,
    safety_s: float = 8.5, escalate_s: float = 25.0, cap_s: float = 300.0,
) -> PolicyResult:
    """Cascaded hedge replay (docs/policies.md).

    Queue cutover at ``cutover_s`` exactly as ``evaluate_queue_cutover``; then,
    if the sole surviving attempt has produced nothing by ``escalate_s`` from
    request start, the fallback provider launches. Replay approximation: on
    the kept-primary branch both primary and hedge stay live (two-job cap), so
    escalation is modeled only on the cutover branch where the primary was
    cancelled — matching the live engine's capacity rule. The loser is
    idealized as cancelled at winner time; a queued cancel bills nothing.
    """

    p_prov = config.provider(primary)
    h_prov = config.provider(hedge)
    f_prov = config.provider(fallback)
    c_p, c_h, c_f = (p_prov.billed_rate_per_s, h_prov.billed_rate_per_s,
                     f_prov.billed_rate_per_s)
    idle_p = p_prov.idle_billed_seconds * c_p
    idle_h = h_prov.idle_billed_seconds * c_h
    idle_f = f_prov.idle_billed_seconds * c_f
    lats: list[float] = []
    costs: list[float] = []
    billeds: list[float] = []
    switches = 0
    for r in rounds:
        rp = r.latency.get(primary, INF)
        rh = r.latency.get(hedge, INF)
        rf = r.latency.get(fallback, INF)
        q_p = r.queue_delay_s.get(primary, INF if math.isinf(rp) else 0.0)
        if q_p > cutover_s:
            # cutover: cancel unstarted primary (free), hedge from cutover_s
            switches += 1
            h_done = cutover_s + rh
            f_done = escalate_s + rf
            if h_done <= escalate_s:
                latency = h_done
                cost = c_h * (r.billed.get(hedge, cap_s) if math.isinf(rh) else rh)
                billed = cost + idle_h
            else:
                # hedge still out at the escalation point -> fallback launches
                latency = min(h_done, f_done)
                if math.isinf(latency):
                    cost = (c_h * r.billed.get(hedge, cap_s)
                            + c_f * r.billed.get(fallback, cap_s))
                    billed = cost + idle_h + idle_f
                else:
                    cost = c_h * min(r.billed.get(hedge, cap_s),
                                     latency - cutover_s)
                    cost += c_f * max(0.0, latency - escalate_s)
                    billed = cost + idle_h + idle_f
        elif rp <= safety_s:
            latency = rp
            cost = c_p * min(r.billed.get(primary, cap_s), rp)
            billed = cost + idle_p
        else:
            # kept primary + safety hedge: two live jobs, no escalation
            switches += 1
            latency = min(rp, safety_s + rh)
            if math.isinf(latency):
                cost = (c_p * r.billed.get(primary, cap_s)
                        + c_h * r.billed.get(hedge, cap_s))
                billed = cost + idle_p + idle_h
            else:
                cost = c_p * min(r.billed.get(primary, cap_s), latency)
                cost += c_h * max(0.0, latency - safety_s)
                billed = cost + idle_p + idle_h
        lats.append(latency)
        costs.append(cost)
        billeds.append(billed)
    return _summarize(
        f"cascade:{primary}->{hedge}->{fallback}"
        f"@q{cutover_s:g}s+s{safety_s:g}s+e{escalate_s:g}s",
        lats, costs, billeds, deadlines, hedges=switches, n=len(rounds),
    )


def _summarize(name, lats, costs, billeds, deadlines, *, hedges, n) -> PolicyResult:
    finite = [x for x in lats if not math.isinf(x)]
    miss_counts = {d: sum(1 for x in lats if x > d) for d in deadlines}
    return PolicyResult(
        name=name, n=n,
        p50_s=_pct(lats, 0.50), p95_s=_pct(lats, 0.95),
        max_s=max(finite) if finite else INF,
        miss_rates={d: (k / n if n else 1.0) for d, k in miss_counts.items()},
        miss_counts=miss_counts,
        mean_cost_usd=sum(costs) / n if n else 0.0,
        mean_billed_usd=sum(billeds) / n if n else 0.0,
        hedge_rate=hedges / n if n else 0.0,
    )


def standard_policy_sweep(
    config: BenchmarkConfig, rounds: list[Round],
    delays_s: tuple[float, ...] = (0, 5, 10, 15, 20, 30, 45, 60),
) -> list[PolicyResult]:
    """The plan's candidate families over one trace set."""

    deadlines = [int(d) for d in config.slo.get("report_deadlines_s", [30, 60, 90, 120])]
    providers = list(config.providers)
    results = [evaluate_single(config, rounds, p, deadlines) for p in providers]
    for primary in providers:
        has_queue_signal = any(primary in r.queue_delay_s for r in rounds)
        for hedge in providers:
            if primary == hedge:
                continue
            for d in delays_s:
                results.append(
                    evaluate_hedge(config, rounds, primary, hedge, d, deadlines)
                )
            if has_queue_signal:
                results.append(
                    evaluate_queue_cutover(config, rounds, primary, hedge, deadlines)
                )
                for fallback in providers:
                    if fallback in (primary, hedge):
                        continue
                    results.append(
                        evaluate_cascade(config, rounds, primary, hedge,
                                         fallback, deadlines)
                    )
    return results
