# Results — pre-registered queue-cutover validation

Run: 2026-07-12, 60 requests in 12 randomized five-request blocks, followed
by a 12-attempt forced-cancellation audit. The design and hypotheses were
committed before collection in [preregistration.yaml](preregistration.yaml).
Traces are committed in [`../../traces/`](../../traces/) (sanitized: job ids
remapped, absolute balances withheld — all latencies, receipts, and spend
deltas untouched, so every number reproduces); reproduce the table and
verdicts with `python analysis.py`.

## Policy outcomes

| arm | n | p50 | p90 | p95 (nearest-rank / linear) | max | miss >60 s (Wilson 95% CI) | hedge/switch rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| single RunPod | 20 | 6.5 s | 175.6 s | 190.9 / 191.4 s | 200.8 s | 7/20 (18–57%) | 0/20 |
| fixed hedge at 10 s | 20 | 29.8 s | 30.8 s | 31.0 / 31.0 s | 31.2 s | 0/20 (0–16%) | 15/20 |
| **queue cutover** | 20 | **22.6 s** | **25.8 s** | 28.2 / **32.0** s | 104.2 s | 1/20 (1–24%) | 20/20 (19 cutovers + 1 safety hedge) |

Queue cutover substantially improved typical latency (median 22.6 s vs
29.8 s, p90 25.8 s vs 30.8 s) and reduced RunPod-side billed cost. Its p95
advantage, however, is **estimator-sensitive and therefore inconclusive**:
under the nearest-rank order statistic the original report used, cutover wins
28.2 s vs 31.0 s; under the linear-interpolation estimator that NumPy and
most analysis packages default to, it loses 32.0 s vs 31.0 s — because with
n=20 the p95 sits between the 19th observation (28.2 s) and the 104.2 s
outlier. The preregistration did not fix the quantile estimator, so we do
not claim the p95 hypothesis as a pass (see H1 below).

The 104.2-second request is the main falsifying observation: it correctly
abandoned the queued primary, then hit the hedge provider's own tail. It
motivated the cascaded policy
([`../2026-08-cascade/preregistration.yaml`](../2026-08-cascade/preregistration.yaml)).
For future experiments the estimator is preregistered, and p95 is not a
primary endpoint at n≈20 — it is determined by one or two requests; median,
p90, max, and miss counts are.

## Cost

Provider-reported/modeled active-compute cost per request was $0.01513 for
queue cutover, $0.01284 for fixed hedging, and $0.01326 for single RunPod.
The long Cerebrium outlier made the proposed active-cost ordering fail.

After subtracting network-volume storage accrued during each block, the
RunPod side cost $0.00532/request for queue-cutover blocks, $0.00649 for
fixed-hedge blocks, and $0.01475 for single-RunPod blocks. These are
RunPod-side costs, not total multi-provider bills; Cerebrium exposes no
account-level usage API.

The full benchmark plus validation and cancellation audit ended at a
projected ledger total of **$7.73**, well below the $50 budget ceiling.

## Registered hypotheses

| hypothesis | verdict | evidence |
| --- | --- | --- |
| H1: queue-cutover p95 ≤ fixed-hedge p95 | **INCONCLUSIVE (estimator-sensitive)** | nearest-rank: 28.2 ≤ 31.0 s; linear interpolation: 32.0 > 31.0 s. The preregistration did not fix the estimator, so no pass is claimed |
| H2: queue-cutover has 0/20 misses over 60 s | **FAIL** | 1/20, the 104.2 s Cerebrium outlier |
| H3: active cost orders cutover < fixed < single | **FAIL** | $0.01513, $0.01284, $0.01326 |
| H4: RunPod billed delta is lower for cutover than single | **PASS** | $0.00532/request < $0.01475/request |
| H5: queued RunPod cancels avoid execution billing | **PASS, with legacy caveat** | 15/15 post-fix cutover receipts reported 0 ms/$0; the forced audit added one `IN_QUEUE` cancel with 0 ms. Four earlier receipts used a wall-time estimate and are explicitly unreconciled. |

## Design limitations (acknowledged post hoc)

- **Block dependence.** The 60 requests ran as 12 single-arm blocks of five,
  four blocks per arm, and provider behaviour was strongly correlated within
  a block (one single-RunPod block contained five slow requests). Treating
  the 20 requests per arm as independent makes the Wilson intervals above
  optimistic; the effective independent sample size is closer to the number
  of blocks (4 per arm) than to 20. The block design was chosen for billing
  attribution, which genuinely needs pure blocks — but it should not have
  been reused for the latency comparison.
- **Fix going forward:** latency arms are interleaved at request level
  (every arm shares provider conditions) and billing runs separately in pure
  blocks — both preregistered in
  [`../2026-08-cascade/preregistration.yaml`](../2026-08-cascade/preregistration.yaml),
  along with the exact quantile estimator and tail-insensitive primary
  endpoints.
- **Operating window.** The queue-cutover arm switched on 20/20 requests
  (19 cutovers + 1 safety hedge): in this cold-heavy window the policy was
  mostly acting as "wait 2.5 s, then use the hedge", so the validation did
  not strongly exercise its keep-the-FlashBoot-fast-path behaviour. A
  multi-regime sweep (steady cadence, idle gaps, post-redeploy) is part of
  the next experiment.

## Forced cancellation audit

- **RunPod:** 4/4 reached `CANCELLED`, no leaks, 250–259 ms acknowledgements.
  The one attempt caught in `IN_QUEUE` reported 0 ms execution; running
  attempts reported 1.1–1.8 seconds.
- **Modal:** the first audit pass failed 4/4 — every cancel was rejected by
  the API (`FunctionCallCancel request must have a function_call_id and
  terminate_containers must be false`) and the adapter's client-side-only
  fallback never fired, so nothing was actually cancelled (an adapter bug
  the audit existed to catch; failed cancels are now recorded as leaks).
  After the fix, a **re-audit succeeded 4/4** via plain input-cancel:
  264–320 ms acknowledgements, zero leaks, one attempt landing on an
  already-completed call. Modal cancellation is input-granular — the
  container idles to its scaledown window, so that idle tail keeps billing.
- **Cerebrium:** both early attempts leaked; both 10-second late attempts
  closed the request channel and were treated as terminal, with 1.1–2.3
  second acknowledgements. The status API still returned the ambiguous
  `Run not found in running state` string.

The audit supports the RunPod policy economics while rejecting any blanket
claim that loser cancellation is equally observable or reliable on all
three providers.
