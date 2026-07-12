# Cancellation semantics

Cancelling the loser is what separates hedging from doubling your bill — and
"we sent a DELETE" is not cancellation. GPUHedge's contract:

## Evidence levels

Acknowledgment and terminal-state confirmation are **distinct events**, and
receipts grade what was actually observed:

| `evidence` | meaning | who can earn it |
| --- | --- | --- |
| `confirmed_terminal` | job polled to a terminal state after the cancel | RunPod, generic HTTP, simulator |
| `provider_ack` | the cancel call was acknowledged; no pollable terminal state exists | Modal (input-cancel) |
| `request_channel_closed` | the in-flight sync request broke when the handler stopped | Cerebrium |
| `no_evidence` | nothing observable (no job id, cancel call failed/raised) | any failure path |

Receipts are **conservative by default**: `evidence` starts at
`no_evidence` and `leaked` starts `true`; adapters flip them only on proof.
An unconfirmed cancellation is never reported as success — the exact failure
mode the forced audit caught in our own Modal adapter.

## The receipt

Every cancel returns a `CancellationReceipt`:

| field | meaning |
| --- | --- |
| `evidence` | the strongest level the adapter earned (table above) |
| `cancel_scope` | `queued_job` / `request` / `container` / `unknown` — what actually stopped. Modal's input-cancel is `request`-scoped: the container idles on and keeps billing |
| `confirmed_terminal` | a terminal state was observed, not assumed |
| `billing_stop_confirmed` | the provider's final billed execution was captured (e.g. RunPod `executionTime`) |
| `cancel_sent_ms` | when the cancel call left the client |
| `cancel_ack_ms` | provider acknowledged the call |
| `terminal_ms` / `terminal_status` | set ONLY from an observed terminal state, never from the cancel call's HTTP response |
| `was_running` | lifecycle phase at cancel time (status is polled just before cancelling) |
| `execution_ms_before_cancel` | how long the loser ran before stopping |
| `estimated_cost_usd` / `reconciled_cost_usd` | modeled vs provider-reported wasted spend |
| `leaked` | `true` unless stopping was evidenced within the poll window |

Leaks are first-class data, not exceptions.

## Cancellation phases

- **Queued cancel** — the job never reached a worker. On providers that bill
  from worker start this should cost nothing; the queue-cutover policy's
  economics depend on it, so the claim is measured against account balance
  deltas (see `benchmarks/2026-07-queue-cutover/preregistration.yaml`,
  hypothesis H5) rather than assumed.
- **Running cancel** — billing should stop at terminal. RunPod exposes the
  loser's final `executionTime`, which the adapter writes into
  `reconciled_cost_usd`.

## Live evidence so far (2026-07)

- 3/3 losing jobs in the live hedging stage were cancelled with 254–309 ms
  acks and 322–388 ms to confirmed terminal, zero leaks — all three were
  RunPod losers.
- The queue-cutover validation produced 19 queued RunPod cutovers. The 15
  receipts written after the receipt-semantics fix all reported 0 ms/$0;
  four earlier wall-estimate receipts remain explicitly unreconciled.
- The forced audit confirmed RunPod terminal cancellation on 4/4 attempts,
  including one `IN_QUEUE` job at 0 ms execution. Cerebrium's two early
  attempts leaked; both late attempts closed the request channel and were
  treated as terminal. Modal's first audit pass failed 4/4 — the API
  rejects `terminate_containers=True` and the adapter's fallback never
  fired; after the fix, a re-audit succeeded 4/4 via input-cancel (264–320
  ms acks, zero leaks), with the container idling to its scaledown window.
  See `benchmarks/2026-07-queue-cutover/results.md`.

## Failure policy

If a cancel fails or leaks, the engine still returns the winner — the user's
request is never held hostage to loser cleanup. The leak is recorded in the
trace and shows up in `gpuhedge report`; a production deployment should
alert on any nonzero leak rate and reconcile against provider billing.

The inverse also holds: a cutover's "cancelled" primary stays in the race
until its cancellation is confirmed. If the remote cancel fails or stays
ambiguous and the hedge then fails or returns invalid output, the primary's
own result can still win — the router never returns nothing while a job it
failed to stop completes remotely. A cancel call that *raises* becomes a
`no_evidence`/leaked receipt instead of an exception.
