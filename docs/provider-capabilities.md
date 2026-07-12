# Provider capabilities & quirks

What each adapter can honestly observe and do, plus the platform behaviours
we hit while building the 2026-07 benchmark. "Verified live" means exercised
against the real provider with traces committed in this repo.

## Capability matrix

| Capability | RunPod (Flash) | Modal | Cerebrium | http adapter |
| --- | --- | --- | --- | --- |
| Queue vs running state | ✅ `IN_QUEUE`/`IN_PROGRESS` | ❌ (pending = running) | ⚠️ `processing` only | per service |
| Queue-cutover capable | ✅ | ❌ | ❌ | if states exposed |
| Queued cancel | ✅ verified live, 0 ms execution | — (no queued state) | ❌ early audit leaked 2/2 | per service |
| Running cancel | ✅ audit 2/2 | ✅ input-cancel 4/4 (re-audit; see below) | ⚠️ late audit 2/2 | per service |
| Terminal proof after cancel | ✅ status poll, 4/4 audit | ⚠️ ack-based (264–320 ms), no status poll | ⚠️ late 2/2; early 0/2 | status poll |
| Per-job billed time | ✅ `executionTime` | ❌ (billing API per app/day) | ✅ `run_time_ms` | per service |
| Account-level spend API | ✅ GraphQL balance | ✅ `Workspace.billing.report` | ❌ none | per service |

The forced loser-cancel audit (`gpuhedge cancel-audit --go`, traces in
`traces/cancel_audit.jsonl`) exercised two early and two late attempts per
provider. RunPod supplied terminal proof on 4/4; Cerebrium's two early
attempts leaked while both late attempts closed the request channel. Modal's
first audit pass failed 4/4 with an uncaught API rejection (an adapter bug —
see quirk 8); after the fix, a re-audit succeeded 4/4 via input-cancel with
264–320 ms acks and zero leaks. Full results:
`benchmarks/2026-07-queue-cutover/results.md`.

## Adapter-specific limits, stated plainly

- **Modal cancellation is INPUT-granular in practice.** Modal's current API
  rejects `FunctionCall.cancel(terminate_containers=True)` outright
  (`"FunctionCallCancel request must have a function_call_id and
  terminate_containers must be false"`, observed live 2026-07-12), so the
  adapter cancels the call and the container idles until its scaledown
  window (20 s in the benchmark deploy) — billing for that idle tail is not
  stopped by the cancel. The receipt's `note` records which mode ran.
- **The Cerebrium adapter is benchmark-safe, not concurrency-safe.** Its
  async API never reconciles run status (see quirks), so the adapter runs
  sync-first and discovers the run id by listing recent runs and matching
  the newest invocation. With `max_replicas=1` and sequential submission
  that is unambiguous; under concurrent traffic it could cancel the wrong
  run. Do not use it in a multi-tenant router until the invocation returns
  an authoritative run id or a correlation token can be matched.

## Platform quirks (each cost a debugging cycle)

1. **RunPod FlashBoot can resume pre-redeploy code.** After a redeploy,
   EXITED-but-cached workers came back running the *old* bundle. Fix:
   terminate the endpoint's pods (GraphQL `podTerminate`) after deploying.
2. **RunPod bills the idle window.** Billing runs from worker start until
   full stop, including the configured `idle_timeout` after each job. At a
   60 s idle timeout, the benchmark's balance delta was ~3× the summed
   per-job `executionTime`. Cost models based on execution time alone
   understate RunPod substantially.
3. **Cerebrium's async API is unusable for result retrieval.**
   `?async=true` returns a run id, but run status stays `processing` with
   `runtimeMs: 0` indefinitely — even after the app logs success. The
   adapter is sync-first; the response body carries
   `{run_id, result, run_time_ms}`.
4. **Cerebrium's parameter validator dies on stringified annotations.** No
   `from __future__ import annotations` (or `str | None` unions) in
   entrypoint signatures, or every call fails with an `isinstance()` crash
   (HTTP 587).
5. **Cerebrium sessions are ~4 h Cognito JWTs**, and the inference gateway
   rejects the session token entirely — it needs the project's
   `cerebrium_jwt` API key. The adapter refreshes the session headlessly
   from the stored refresh token and fetches the inference key on demand.
6. **transformers 5.x removed `TRANSFORMERS_CACHE`** — an innocent import
   crash-looped a container long enough to bill real money before diagnosis.
   Pin and smoke-test worker images.
7. **Modal `already_loaded` can't distinguish cold from warm** when the
   model loads in an `@enter` hook — wall time carries the cold signal
   instead.
8. **Modal rejects `terminate_containers=True`** on FunctionCall cancels
   (server-side, not a client TypeError — the adapter's original fallback
   never fired, so the first forced audit failed 4/4 before anyone noticed).
   A failed cancel is recorded as `leaked: true` now; the working path is a
   plain input-cancel.

## Rates used in the benchmark (2026-07, US regions)

| Provider | GPU | headline $/hr | all-in $/s used |
| --- | --- | ---: | ---: |
| RunPod serverless | RTX 4090 24 GB | ~$1.10 | 0.000306 (CPU/RAM bundled) |
| Modal | L40S 48 GB | ~$1.95 | 0.000704 (+ CPU/RAM billed separately) |
| Cerebrium | L40S 48 GB | ~$1.95 | 0.000736 (all-in with 8 vCPU / 48 GB) |

Rates drift; reconcile against provider billing after every block
(`gpuhedge costs` does this automatically at block boundaries).
