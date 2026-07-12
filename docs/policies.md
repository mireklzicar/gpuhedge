# Policies

A policy decides *when* additional jobs launch and *how* the race resolves.
All policies share the invariants: at most two live jobs at any moment, the
first result that passes validation wins, and every loser is cancelled with
an evidence-graded receipt.

Policies are plain objects dispatched through one protocol method:

```python
class Policy(Protocol):
    min_providers: int
    async def execute(self, ctx: RoutingContext) -> dict[str, Any]: ...
```

The `RoutingContext` carries the config, ledger, trace writer, the ordered
provider tuple (primary, hedge, fallback, …), and the request id. Implement
`execute` on your own class and the Router runs it exactly like the
built-ins — there are no isinstance checks to fall through.

## SingleProvider

No hedging. The baseline every benchmark table measures against — and the
right choice when the primary's tail is acceptable or the workload is batch.

## FixedHedgePolicy(hedge_after_ms=10_000)

The classic tail-hedge (Dean & Barroso's "The Tail at Scale", applied to
whole GPU jobs): if the primary has not produced a valid result after
`hedge_after_ms`, launch one hedge and let them race.

Choosing the delay is a latency/cost trade the offline replay makes cheap:

- too small → you duplicate nearly every request (the immediate race is the
  latency floor, at ~100% hedge rate);
- too large → the hedge starts so late that misses blow the SLO anyway.

On the 2026-07 dataset the knee was at ~10 s: 0/36 misses at a 31% hedge
rate, *cheaper* in active-compute terms than not hedging, because a cancelled
loser costs less than an unhedged 100 s tail.

## StateAwarePolicy(queue_cutover_ms=2_500, safety_hedge_ms=8_500)

The fixed hedge waits out the delay even when the outcome is already
knowable. On providers that expose real lifecycle states, the primary's
**queue state** predicts its slow mode much earlier: in 54 measured rounds,
fast-path requests spent 1.1–2.0 s in queue and fresh-worker requests
8.9–27.6 s — an empty gap around 2.5 s.

```
t = 0        submit primary
t = 2.5 s    poll primary state
               IN_QUEUE     → cancel primary (its worker never started),
                              launch hedge — full switch
               IN_PROGRESS  → keep primary (likely fast path)
t = 8.5 s    still no valid result → launch hedge as a safety fallback
             first valid result wins; cancel the loser
```

Two properties matter:

1. **It reroutes before paying.** A queued cancel stops the job before a
   worker starts; on providers that bill from worker start, the abandoned
   tail costs (approximately) nothing. This billing claim is measured, not
   assumed — see `benchmarks/2026-07-queue-cutover/`.
2. **The safety hedge covers the misclassification risk.** A primary that
   was running at the poll but is still grinding at 8.5 s gets the same
   protection the fixed hedge provides.

## CascadePolicy(queue_cutover_ms=2_500, safety_hedge_ms=8_500, escalate_after_ms=25_000)

The state-aware cutover has one blind spot the 2026-07 validation exposed
live: **the hedge provider has tails too**. One request correctly abandoned
its queued primary at 2.5 s and then spent 104.2 s waiting on the hedge.

The cascade adds a second-level fallback:

```
t = 0        submit primary
t = 2.5 s    poll primary state — cutover exactly as StateAwarePolicy
t = 8.5 s    safety hedge exactly as StateAwarePolicy
t = 25 s     no valid result AND fewer than two attempts still live
             → submit the fallback provider
always       first valid result wins; every loser cancelled with a receipt;
             never more than two live jobs
```

Properties:

1. **Escalation is free when unneeded.** Replayed over the 36 evaluation
   rounds, the hedge never stalled past the escalation point, so the cascade
   produced numbers identical to the cutover — the third provider is
   contacted only when the earlier stages are already unhealthy.
2. **It bounds the hedge tail; it does not erase it.** With Modal's observed
   cold distribution (p50 ≈ 38 s) as the fallback, escalating at 25 s turns
   the recorded 104.2 s outlier into ≈ 63 s (median counterfactual) — a much
   better worst case, but not a guaranteed 60 s SLO save. The pre-registered
   live experiment (`benchmarks/2026-08-cascade/`, not yet run) states this
   expectation up front.
3. **The two-job cap is enforced by liveness, not hope.** An attempt counts
   as live until its result task finishes or its cancellation is confirmed
   terminal; escalation waits (and records that it waited) while two
   attempts are genuinely live.

A higher-reliability variant for critical requests — cancel the queued
primary and race hedge + fallback immediately — costs more on exactly the
predicted-bad paths and is on the roadmap as a `deadline`-style tier.

## Failure containment (all hedging policies)

A remote cancel that fails or stays ambiguous never discards the local
result task: the "cancelled" primary stays in the race until proof arrives,
so a failing hedge can still be rescued by the very job the policy tried to
abandon. A cancel that raises becomes a `no_evidence`/leaked receipt rather
than an error that loses an already-won race.

## Choosing parameters from your own traces

Run your workload through `gpuhedge bench` (or collect traces any other
way), then replay policy families offline:

```python
from gpuhedge.benchmark.replay import load_rounds, standard_policy_sweep
rounds = load_rounds("traces/moss_rounds.jsonl")
for r in standard_policy_sweep(config, rounds):
    print(r.to_record())
```

The sweep evaluates singles, immediate races, fixed hedges over a delay
grid, queue cutovers, and cascades (where a queue signal exists) with both
cost models (active-compute and idle-inclusive billed).

## Freezing and validating

Anything tuned on a trace set is *calibrated*, not validated. The discipline
this repo follows and recommends:

1. calibrate on one block of data;
2. write the exact policy into a preregistration YAML and commit it;
3. run a fresh randomized experiment (`gpuhedge validate --go`);
4. report only the pre-registered metrics, with counts and intervals.
