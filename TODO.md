# TODO / Roadmap

Near term (order reflects the current plan, not promises):

1. **Queue-cutover validation results** — the pre-registered 60-request
   randomized experiment (latency + account-billed cost + forced
   loser-cancels on all three providers): `benchmarks/2026-07-queue-cutover/`.
2. **Cadence-aware routing** — FlashBoot hit rates depend on traffic
   regularity (67% steady vs 2/7 idle-heavy); route on time-since-last-request.
3. **Conditional hedge selection** — score hedges by
   P(finish ≤ remaining deadline | primary state, recent conditions) per
   incremental dollar, with uncertainty penalties for thin data.
4. **More adapters** — fal, Replicate (or your service via `adapter: http`).
5. **Workload generalization** — a small LLM (vLLM) and Whisper as the
   negative control (a 0.8 B model should rarely justify a hedge).
6. **A recurring cold-start dataset** — the same harness re-run periodically
   across providers/regions/cadences, published as data.

Deliberately out of scope for now: unified billing,
multi-tenant concurrency (the Cerebrium adapter is benchmark-safe only —
see docs/provider-capabilities.md).
