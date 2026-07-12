# Architecture

GPUHedge is three layers with one contract between them.

```
            Router (router.py)                     public API
                │  policy value (policies/)
                ▼
   Policy engines (benchmark/)                     when to hedge, who wins
     live_hedge.run_hedged_request      fixed timer hedge
     state_aware.run_state_aware_request queue-state cutover + safety hedge
     validation / round / controller     benchmark drivers
                │  Backend contract (backends/base.py)
                ▼
   Provider adapters (backends/)                   how to talk to a cloud
     runpod · modal · cerebrium · http · sim
```

## The Backend contract

Every adapter implements four calls and never lies about what it can see:

```python
handle = await backend.submit()          # returns a JobHandle immediately
state  = await handle.status()           # QUEUED / IN_PROGRESS / terminal
result = await handle.result(timeout_s)  # ProviderResult (state, wall_s, audio, metrics)
receipt = await handle.cancel()          # CancellationReceipt
```

- `status()` must be cheap enough to poll (the cutover policy calls it once
  at ~2.5 s). Providers whose API cannot distinguish queued from running
  return the coarser truth (Modal reports `IN_PROGRESS` for anything
  pending); they are still raceable, just not cutover-capable.
- `result()` right-censors at the caller's cap and returns a
  `TIMEOUT`-state result rather than raising.
- `cancel()` never treats an HTTP 200 as proof. Adapters poll to a confirmed
  terminal state and fill in the receipt (`cancel_sent/ack/terminal_ms`,
  `was_running`, `execution_ms_before_cancel`, `estimated_cost_usd`,
  `leaked`). A cancel that cannot be confirmed sets `leaked: true`.

Adapters are selected by `extra.adapter` in the provider config (defaulting
to the provider key), so configs can declare any number of providers backed
by the generic `http` or `sim` adapters.

## The policy engines

Engines own the race logic and know nothing about specific providers:

- **fixed hedge** (`live_hedge.py`): give the primary `hedge_after_ms`; if no
  *valid* result by then, launch one hedge; first valid wins; cancel the
  loser. At most two active jobs.
- **state-aware cutover** (`state_aware.py`): poll the primary's state at
  `queue_cutover_ms`. Still queued → cancel it before a worker starts and
  switch to the hedge. Running → keep it, but arm a safety hedge at
  `safety_hedge_ms`.

Winners are decided by the configured **validator** (`validators/registry.py`)
— a fast malformed response keeps the race open.

All engines write one JSONL record per request with end-to-end latency
(`winner_total_ms`), the decision fields (`state_at_poll`, `cutover_fired`,
`safety_hedge_fired`), and the loser's receipt. Those traces are the input to
the offline replay (`benchmark/replay.py`), which re-evaluates whole policy
families from the same data without spending GPU money.

## Telemetry

- `telemetry/trace.py` — append-only JSONL, one file per stage.
- `telemetry/ledger.py` — projected-cost ledger; every charge passes through
  hard budget gates and raises `BudgetExceeded` past the operational stop.
  Resumable from its own file.
- `telemetry/costs.py` — provider-reported actuals (balance/billing APIs
  where they exist), snapshotted at block boundaries and reconciled against
  the ledger.

## Determinism & tests

The `sim` backend replays deterministic per-provider draw streams through the
real engines, so the integration tests (and `gpuhedge demo`) exercise exactly
the code that runs against clouds — cutover-while-queued, safety-hedge
rescue, malformed-result rejection, receipt production — in milliseconds,
offline.
