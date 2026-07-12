# Contributing

Thanks for looking under the hood. The fastest ways to help:

- **A new provider adapter** — see [docs/adding-a-provider.md](docs/adding-a-provider.md).
  fal and Replicate are the most-requested next targets.
- **Benchmark replications** — run `gpuhedge bench --go` on your own
  accounts/regions/workloads and share sanitized traces; provider behaviour
  varies over time and geography, and independent datasets are the most
  valuable contribution of all.
- **Policies** — implement a policy dataclass + engine, with a replay
  evaluator so it can be tested against committed traces before it ever
  spends money.

## Ground rules

1. `make dev` to set up, `make lint && make test` before pushing. CI runs
   ruff + pytest on Python 3.10–3.12 plus a wheel smoke test.
2. Tests must not require cloud credentials — use the simulator
   (`backends/sim_backend.py`) or fake handles. Live behaviour goes behind
   `--go` CLI paths, never into pytest.
3. Honesty over polish: adapters must not fabricate lifecycle states,
   cancels must poll to terminal or set `leaked`, and any published number
   must say which cost model it comes from
   ([docs/cost-accounting.md](docs/cost-accounting.md)).
4. Keep the public surface small. New user-facing API needs a short design
   note in the PR description explaining why the Router/policies/validators
   surface can't already express it.

## Reporting bugs

Include the trace record (sanitized) for the affected request when possible —
`traces/*.jsonl` lines are self-contained and are usually enough to diagnose
adapter issues without your credentials or endpoints.
