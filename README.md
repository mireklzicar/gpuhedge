# GPUHedge

**State-aware speculative execution for serverless GPUs.**

> First valid result wins. Loser cancellation is audited — and the receipt
> says when the cloud didn't stop.

Serverless GPU cold starts are wildly bimodal: the same 17 GB TTS model
returns complete audio in ~6 s on a warm-path hit but 89–122 s on a miss —
with nothing in between. In 36 evaluation rounds across three real providers,
a delayed hedge cut p95 cold-start latency from **116.6 s to 29.4 s**, cut
60-second deadline misses from **11/36 to 0/36**, and launched a second job on
only **11/36** requests.

The sharper discovery: the primary provider's **queue state at 2.5 seconds**
predicted every 90–122 s tail with zero overlap in 54 rounds — early enough
to cancel the queued job *before its worker starts* and reroute. A
pre-registered live validation found the cutover reduced median latency from
29.8 s to 22.6 s and cut RunPod-side billed cost from $0.01475 to
$0.00532/request — but one 104.2 s *hedge-provider* tail made its p95
advantage estimator-sensitive at n=20 (28.2 s vs 31.0 s under nearest-rank,
32.0 s vs 31.0 s under linear interpolation), so we don't claim that
hypothesis. The outlier motivated the **cascaded policy** below. Full
results are under
[`benchmarks/2026-07-queue-cutover/`](benchmarks/2026-07-queue-cutover/).

![queue delay predicts the tail](benchmarks/2026-07-moss/figures/fig1_queue_delay_hero.png)

## What it does

GPUHedge races your *own* deployments (containers, weights, custom code)
across serverless GPU clouds under an SLO policy:

1. submit the request to the cheap/fast **primary**;
2. watch the primary's **lifecycle state** (queue vs running), not just a timer;
3. launch a **hedge** only when the primary is entering its tail — and
   **escalate to a third provider** when the hedge enters its own
   (`CascadePolicy`, at most two live jobs);
4. return the **first result that passes validation** (a fast HTTP 200 with
   malformed output does not win);
5. **cancel every loser** through its provider-native job API and record a
   structured `CancellationReceipt` with an explicit **evidence level**
   (`confirmed_terminal` > `provider_ack` > `request_channel_closed` >
   `no_evidence`), cancellation scope, ack latency, estimated wasted GPU-$,
   and a leak flag that defaults to *leaked* until proof arrives.

## Try it in 60 seconds — no accounts, no spend

```bash
pip install -e .
gpuhedge demo                              # simulated providers, real policy engines
gpuhedge replay traces/moss_rounds.jsonl   # reproduce the benchmark tables
```

The demo races a bimodal primary against a steady hedge through the exact
code paths used against real clouds — including a malformed-result round the
validator catches and a queued-cancel cutover.

## The public API

```python
from gpuhedge import Router
from gpuhedge.policies import StateAwarePolicy

router = Router(
    primary="runpod", hedge="cerebrium",
    policy=StateAwarePolicy(queue_cutover_ms=2_500, safety_hedge_ms=8_500),
)
outcome = await router.run()
outcome.winner          # "runpod" | "cerebrium"
outcome.total_ms        # end-to-end latency from request start
outcome.cancellation    # the loser's receipt (or None if no hedge launched)
```

Policies: `SingleProvider()`, `FixedHedgePolicy(hedge_after_ms)`,
`StateAwarePolicy(queue_cutover_ms, safety_hedge_ms)`, and
`CascadePolicy(queue_cutover_ms, safety_hedge_ms, escalate_after_ms)` (pass
`fallback="modal"` to the Router). Policies are dispatched through one
protocol method — implement `async def execute(self, ctx)` plus
`min_providers` and your own policy runs exactly like the built-ins.
Validators are pluggable (`wav`, `json`, `nonempty`, or
`register_validator("mine")(...)`).

Providers are YAML: built-in adapters for **RunPod** (Flash queue SDK +
REST), **Modal** (`FunctionCall.spawn`/`cancel`), **Cerebrium** (sync-first
REST), a **simulator** (`adapter: sim`), and a **generic HTTP adapter**
(`adapter: http`) that turns any submit/status/result/cancel service into a
raceable backend without writing Python — see
[`docs/adding-a-provider.md`](docs/adding-a-provider.md).

## The benchmark (2026-07, MOSS-TTS-v1.5, three providers)

Same 17 GB model, same commit, same request, pre-seeded weights, identical
WAV validation on every arm. 54 paired cold-start rounds + warm companions,
18 live hedged requests, ~$7 of GPU spend, every trace committed under
[`traces/`](traces/) (sanitized: job ids remapped, account balances withheld,
deployment identifiers replaced — every latency, receipt, and cost delta is
untouched, so all tables and figures reproduce identically). Full method and
results: [`benchmarks/2026-07-moss/`](benchmarks/2026-07-moss/). Regenerate
the figures with `pip install -e ".[analysis]" && python
benchmarks/2026-07-moss/analysis.py` (CI does this on every push).

| Policy (36 evaluation rounds) | p50 | p95 | miss>60 s | active $/req | hedge rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| single: primary (RTX 4090) | 6.0 s | 116.6 s | 11/36 | $0.0114 | — |
| fixed hedge @10 s | 6.0 s | 29.4 s | 0/36 | $0.0083 | 11/36 |
| queue cutover @2.5 s (replay*) | 6.0 s | 21.9 s | 0/36 | $0.0056 | 11/36 |
| cascade →modal @25 s (replay*) | 6.0 s | 21.9 s | 0/36 | $0.0056 | 11/36 |

\* the cutover and cascade rows are post-hoc replays on data that informed
them; the cutover's own pre-registered, randomized 60-request live validation
(with account-level billing deltas and forced loser-cancels on all three
providers) is in
[`benchmarks/2026-07-queue-cutover/`](benchmarks/2026-07-queue-cutover/). In
this replay the hedge never stalled, so the cascade's escalation never fired
— identical numbers, tail protection for free.

| Pre-registered live validation (20/arm) | p50 | p90 | max | miss>60 s | RunPod billed $/req |
| --- | ---: | ---: | ---: | ---: | ---: |
| single RunPod | 6.5 s | 175.6 s | 200.8 s | 7/20 | $0.01475 |
| fixed hedge @10 s | 29.8 s | 30.8 s | 31.2 s | 0/20 | $0.00649 |
| **queue cutover @2.5 s** | **22.6 s** | **25.8 s** | 104.2 s | **1/20** | **$0.00532** |

The cutover passed the RunPod-billing and queued-cancel-unbilled hypotheses
and clearly improved typical latency, but its registered p95 hypothesis is
**inconclusive** — at n=20 the verdict flips with the quantile estimator
(28.2 s vs 31.0 s nearest-rank, 32.0 s vs 31.0 s linear) — and it failed the
zero-miss and active-cost-order hypotheses: one correctly-switched request
spent 104.2 s on the hedge provider's own tail. See the
[validation results](benchmarks/2026-07-queue-cutover/results.md).

**That 104.2 s outlier is why the cascade exists**: the primary can have a
tail, but so can the hedge. `CascadePolicy` escalates to a third provider
when the sole surviving attempt has produced nothing by `escalate_after_ms`
(never more than two live jobs). Its live validation is pre-registered — with
the quantile estimator fixed and request-level interleaving this time — in
[`benchmarks/2026-08-cascade/`](benchmarks/2026-08-cascade/preregistration.yaml)
and has not run yet; `gpuhedge demo` shows the mechanism on simulated
providers today.

**Cost honesty**: "active $" is modeled execution-seconds × rate. Real bills
also include idle windows (one provider's balance delta ran ~3× its summed
per-job execution time at a 60 s idle timeout) — both models are reported,
and the validation measures actual balance deltas per policy arm. See
[`docs/cost-accounting.md`](docs/cost-accounting.md).

**Cancellation honesty**: every receipt carries an explicit evidence level —
RunPod cancels are polled to a `confirmed_terminal` state with billing
reconciled; Modal cancels are `provider_ack` only (input-granular: the
container idles to its scaledown window and keeps billing); Cerebrium's
strongest proof is `request_channel_closed`. Anything less is recorded as a
leak, never assumed stopped. The capability matrix in
[`docs/provider-capabilities.md`](docs/provider-capabilities.md) states
exactly which cancel paths have been exercised live on which provider —
including the forced audit that caught our own Modal adapter silently
failing before the fix.

## Run it against real clouds

```bash
pip install -e ".[providers]"     # or a subset: .[runpod], .[modal], .[cerebrium]
mkdir -p config
cp src/gpuhedge/config/benchmark.example.yaml config/benchmark.yaml
# fill in your endpoint ids / app names, set deployed: true
gpuhedge login-check              # verifies auth, spends nothing
gpuhedge plan                     # what the config encodes, incl. budget gates
gpuhedge bench --dry-run          # inspect before spending
gpuhedge bench --go               # Stage 2 paired cold-start rounds
gpuhedge cutover --go             # one live state-aware request
```

Nothing submits a GPU job without `--go`. A projected-cost ledger enforces
hard budget gates (`BudgetExceeded` halts submission), and `gpuhedge costs`
reconciles projections against provider-reported billing.

Deployment recipes for the benchmark model on all three providers are under
[`deploy/`](deploy/) — self-contained, with the provider gotchas we hit
documented in [`docs/provider-capabilities.md`](docs/provider-capabilities.md).

## Repository map

```
src/gpuhedge/
  router.py          Router — the public API
  policies/          SingleProvider / FixedHedgePolicy / StateAwarePolicy /
                     CascadePolicy — plus the execute() protocol for custom ones
  backends/          runpod / modal / cerebrium / sim / generic http adapters
  validators/        wav / json / custom validator registry
  benchmark/         paired rounds, live hedging, state-aware engine,
                     replay (offline policy evaluation), reports, demo
  telemetry/         JSONL traces, projected-cost ledger with budget gates,
                     provider billing monitors
  config/            benchmark.yaml (plan-as-data), demo.yaml
benchmarks/          2026-07-moss (method, results, figures, analysis.py),
                     2026-07-queue-cutover (preregistration + validation),
                     2026-08-cascade (preregistration; not yet run)
deploy/              MOSS-TTS deployment recipes per provider
traces/              committed benchmark traces (JSONL, sanitized)
docs/                architecture, policies, capabilities, cancellation,
                     cost accounting, adding a provider, security
```

## Status & caveats

- Benchmark data is one workload (TTS, ~5–7 s of audio), one region per
  provider, collected across two days in 2026-07. Provider rankings move;
  that is the argument *for* state-aware routing, not against it — treat any
  single number as a snapshot.
- The Cerebrium adapter discovers run ids from the runs list (its async API
  does not return usable handles): **benchmark-safe, not concurrency-safe**
  — see [`docs/provider-capabilities.md`](docs/provider-capabilities.md).
- No p99s are quoted anywhere: n is tens, not thousands. Miss rates carry
  Wilson 95% intervals.

## Contributing

`make dev && make lint && make test`. CI runs Python 3.10–3.12 and a wheel
smoke test. Adapter contributions welcome — the contract is one class with
`submit / result / status / cancel` returning honest lifecycle events, plus
mock tests; see [`docs/adding-a-provider.md`](docs/adding-a-provider.md) and
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache-2.0.
