# GPUHedge

[![CI](https://github.com/mireklzicar/gpuhedge/actions/workflows/ci.yml/badge.svg)](https://github.com/mireklzicar/gpuhedge/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/gpuhedge.svg?color=2dd4bf)](https://pypi.org/project/gpuhedge/)
[![Python](https://img.shields.io/pypi/pyversions/gpuhedge.svg)](https://pypi.org/project/gpuhedge/)
[![License](https://img.shields.io/pypi/l/gpuhedge.svg?color=blue)](https://github.com/mireklzicar/gpuhedge/blob/main/LICENSE)
[![Website](https://img.shields.io/badge/website-gpuhedge.com-2dd4bf.svg)](https://www.gpuhedge.com)

**Reduce serverless-GPU tail latency by launching a backup when the primary is
slow.**

GPUHedge routes the same asynchronous inference request across two or more
serverless GPU backends, returns the first result that passes *your* validator,
and cancels the other attempts through each provider's native API — with an
auditable receipt (evidence level, wasted GPU-$, leak flag).

Built-in backends: **RunPod**, **Modal**, **Cerebrium**, a **generic HTTP**
adapter, and a local **simulator**.

```bash
pip install gpuhedge
gpuhedge demo        # simulated providers, real policy engines — no accounts, no spend
```

<p align="center">
  <img src="assets/gpuhedge_demo.svg" alt="Terminal recording: gpuhedge login-check verifies Modal, RunPod and Cerebrium, then gpuhedge demo races the policies — a slow primary is cancelled before its worker starts, a backup takes over, and p95 drops from 119 s to 21.6 s" width="840">
</p>

> On one 17 GB TTS workload, 36 evaluation rounds across three real providers, a
> 10-second delayed hedge cut **p95 cold-start latency from 116.6 s to 29.4 s**
> and **deadline misses from 11/36 to 0/36 over 60 s** — while *lowering*
> active-compute cost, because a short-lived cancelled backup is cheaper than
> letting a 100-second primary tail run to completion. It launched a backup on
> only **31%** of requests; the fast path is left alone the rest of the time.

**Status: alpha.** The benchmark covers one workload and one region per
provider, collected over two days in 2026-07. Provider rankings move — that is
the argument *for* state-aware routing, not a universal ranking. Treat any
single number as a snapshot.

The demo above races a bimodal primary against a steady hedge through the exact
code paths that run against real clouds — including a malformed result the
validator rejects and a queued-cancel cutover. Try it live and interactive at
**[gpuhedge.com](https://www.gpuhedge.com)**.

## How it works

1. **Submit** to the cheap/fast primary.
2. **Watch** its lifecycle state — queued vs. running, not just a timer.
3. **Hedge** to another provider only when the primary enters its tail — and
   escalate to a third if the hedge stalls too (never more than two live jobs).
4. **Validate** — the first result that passes your validator wins; a fast
   HTTP 200 with malformed output does not.
5. **Cancel** every loser through its provider-native API and record an audited
   receipt.

## Three modes

GPUHedge ships one stable default and two opt-in experimental policies. They
have different risk profiles — choose deliberately:

| mode | policy | behaviour | when to use |
| --- | --- | --- | --- |
| **stable** (default) | `FixedHedgePolicy(hedge_after_ms)` | launch the hedge on a fixed timer (10 s) | a deadline miss is unacceptable |
| experimental | `StateAwarePolicy(queue_cutover_ms, safety_hedge_ms)` | poll the primary's queue state; cancel it *before its worker starts* and switch if still queued | you want the lowest typical latency and can absorb a rare tail |
| experimental | `CascadePolicy(…, escalate_after_ms)` | queue cutover, then escalate to a third provider if the hedge also stalls | multi-provider setups that must survive a slow hedge |

Do not describe the default as lifecycle-aware: the **stable** policy is a plain
10-second timer. Queue-state cutover and cascade are **experimental**.

## Quickstart (Python)

```python
import asyncio
from gpuhedge import Router
from gpuhedge.policies import StateAwarePolicy


async def main():
    # stable mode (default): a 10 s delayed hedge
    router = Router(primary="runpod", hedge="cerebrium")

    # experimental mode: cut over at 2.5 s if the primary is still queued
    router = Router(
        primary="runpod", hedge="cerebrium",
        policy=StateAwarePolicy(queue_cutover_ms=2_500, safety_hedge_ms=8_500),
    )

    outcome = await router.run(timeout_s=60)
    print(outcome.winner)        # "runpod" | "cerebrium"
    print(outcome.total_ms)      # end-to-end latency from request start
    print(outcome.cancellation)  # the loser's audited cancellation receipt
    router.close()


asyncio.run(main())
```

Providers, rates, and your request live in a small YAML file. The `Router` reads
the packaged default or a local `./config/benchmark.yaml`; point it at the
bundled **simulator** providers (`adapter: sim`) to run the code above with no
cloud accounts, exactly as `gpuhedge demo` does.

Custom policies plug in through one method — implement `async def execute(self,
ctx)` and `min_providers`, and yours runs like the built-ins. Validators are
pluggable too (`wav`, `json`, `nonempty`, or `register_validator("mine")(...)`).
See [`docs/policies.md`](docs/policies.md).

## Generic HTTP backends

The `http` adapter turns any submit/status/result/cancel service into a raceable
backend straight from YAML — no Python. You describe how to read each response
with dotted paths (`job_id_path`, `state_path`, …) and map the service's states
onto GPUHedge's:

```yaml
providers:
  my_service:
    role: hedge
    gpu: L40S
    billed_rate_per_s: 0.000542
    deployed: true
    adapter: http
    http:
      headers: { Authorization: "Bearer ${MY_SERVICE_TOKEN}" }   # ${ENV} substituted
      submit: { method: POST, url: https://example.com/jobs,
                body: { input: "{request}" }, job_id_path: id }
      status: { url: "https://example.com/jobs/{job_id}", state_path: status,
                state_map: { queued: QUEUED, running: IN_PROGRESS,
                             succeeded: COMPLETED, failed: FAILED } }
      result: { url: "https://example.com/jobs/{job_id}",
                audio_b64_path: output.audio_base64, poll_interval_s: 1.0 }
      cancel: { method: DELETE, url: "https://example.com/jobs/{job_id}" }
```

```python
router = Router(primary="my_service", hedge="another_http_service",
                config="config/http.yaml")
```

Any two async HTTP endpoints become a hedged pair — you do not need the
provider-specific adapters at all. Cancellation is confirmed by polling status
to a terminal state, not by trusting the cancel call's HTTP 200. Full field
reference is in [`docs/adding-a-provider.md`](docs/adding-a-provider.md).

## Run it against real clouds

```bash
pip install "gpuhedge[providers]"   # or a subset: gpuhedge[runpod], [modal], [cerebrium]

# drop the example config into ./config/ and fill in your endpoint ids /
# app names, then set deployed: true (gpuhedge reads ./config/benchmark.yaml)
mkdir -p config && python -c "import shutil,importlib.resources as r; \
shutil.copy(r.files('gpuhedge')/'config'/'benchmark.example.yaml','config/benchmark.yaml')"

gpuhedge login-check                # verifies auth, spends nothing
gpuhedge plan                       # what the config encodes, incl. budget gates
gpuhedge cutover --go               # one live state-aware request
```

Nothing submits a GPU job without `--go`. A projected-cost ledger enforces hard
budget gates (`BudgetExceeded` halts submission), and `gpuhedge costs`
reconciles projections against provider-reported billing. Deployment recipes for
the benchmark model on all three providers are under [`deploy/`](deploy/).

## Benchmark

The headline numbers come from a reproducible 2026-07 study: the same 17 GB
MOSS-TTS model, same request, identical WAV validation on every arm, 54 paired
cold-start rounds across RunPod, Modal, and Cerebrium. Every (sanitized) trace
is committed, so the tables reproduce with `gpuhedge replay
traces/moss_rounds.jsonl`.

| Policy (36 evaluation rounds) | p50 | p95 | miss >60 s | $/req (active) |
| --- | ---: | ---: | ---: | ---: |
| single: RunPod (cheapest single provider) | 6.0 s | 116.6 s | 11/36 | $0.0114 |
| **fixed hedge → Cerebrium @10 s (stable)** | **6.0 s** | **29.4 s** | **0/36** | **$0.0083** |
| queue cutover @2.5 s (experimental) | 6.0 s | 21.9 s | 0/36 | $0.0056 |

<!-- ![Cold-start latency by policy: single-provider tails vs. the hedged distributions](assets/gpuhedge_boxplot.png) -->

Full method, figures, cost models, and a **pre-registered live validation** (with
account-level billing deltas and forced loser-cancellations on all three
providers) are in [`benchmarks/`](benchmarks/). Live, **experimental** mode won
the typical case outright — median 22.6 s vs 29.8 s (stable), p90 25.8 s vs
30.8 s — and cost less on the primary; the trade is tail risk, where one run
abandoned its queued primary and then hit the hedge provider's own 104 s tail.
That case is exactly what `CascadePolicy` (experimental) escalates past.

## Providers

Built-in adapters: **RunPod** (Flash queue SDK + REST), **Modal**
(`FunctionCall.spawn`/`cancel`), **Cerebrium** (sync-first REST), a
**simulator** (`adapter: sim`), and the **generic HTTP adapter** (`adapter:
http`) above. Adding your own is one class with four methods:
[`docs/adding-a-provider.md`](docs/adding-a-provider.md).

## Docs

[architecture](docs/architecture.md) ·
[policies](docs/policies.md) ·
[provider capabilities](docs/provider-capabilities.md) ·
[cancellation semantics](docs/cancellation-semantics.md) ·
[cost accounting](docs/cost-accounting.md) ·
[adding a provider](docs/adding-a-provider.md)

## Status & caveats

- Benchmark data is one workload (TTS), one region per provider, collected over
  two days in 2026-07. Provider rankings move — that is the argument *for*
  state-aware routing. Treat any single number as a snapshot.
- The Cerebrium adapter is **benchmark-safe, not concurrency-safe** (it
  discovers run ids from the runs list); see
  [`docs/provider-capabilities.md`](docs/provider-capabilities.md).
- n is in the tens: miss rates carry Wilson 95% intervals and no p99s are
  quoted anywhere.

## Contributing

`make dev && make lint && make test`. CI runs Python 3.10–3.12, a wheel smoke
test, `gpuhedge demo`, and regenerates the benchmark figures. Adapter
contributions welcome — see [`docs/adding-a-provider.md`](docs/adding-a-provider.md)
and [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache-2.0.
