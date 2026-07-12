# Cost accounting

Three layers, because every one of them catches errors the others miss.

## 1. Projected ledger (hard gates)

Every submitted job charges `wall_seconds × billed_rate_per_s` (plus the
provider's configured idle window) into an append-only ledger
(`telemetry/ledger.py`). Charges are checked against budget gates from the
config; crossing the operational stop raises `BudgetExceeded` and halts
submission. The ledger is deliberately pessimistic — it exists to stop
runaway spend in real time, not to be precise.

## 2. Provider-reported actuals

`telemetry/costs.py` snapshots whatever each provider exposes at block
boundaries and reconciles it into the ledger:

- RunPod: real-time account balance (GraphQL) + per-job `executionTime`;
- Modal: `Workspace.billing.report` per-app cost (updates intraday);
- Cerebrium: per-run `run_time_ms` only — no account API; reconcile against
  the dashboard after the fact.

## 3. Replay cost models

The offline replay reports **two** numbers per policy, and the difference
matters more than either value:

- **active-compute $/req** — execution-seconds × rate, loser idealized as
  cancelled at winner time. Comparable across policies; NOT the bill.
- **billed $/req** — adds each provider's `idle_billed_seconds` for every
  round in which that provider's worker actually started, and credits a
  primary cancelled while still queued as unbilled.

Why it matters, from the 2026-07 data: at RunPod's 60 s idle timeout the
account balance fell ~3× faster than summed execution time. In
active-compute terms the 10 s hedge was 27% cheaper than single-provider; in
billed terms ~11% — while the queue cutover, which avoids starting the
primary's worker at all on the slow path, kept most of its advantage
(~38% modeled).

## Rules this repo follows when publishing dollar figures

1. Say which model a number comes from (active vs billed vs account delta).
2. Never present execution-time cost as "the bill".
3. Validate billing assumptions (queued-cancel-is-free, idle windows) with
   account-level balance deltas per policy arm — the pre-registered
   validation snapshots the RunPod balance before and after every 5-request
   block with no other account activity, and subtracts storage pro-rata.
4. Reconcile again after provider reporting settles (next day), because
   several providers report billing with lag.
