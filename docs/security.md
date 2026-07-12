# Security

## Credentials

GPUHedge never stores provider credentials itself. Adapters read what the
provider CLIs already manage, or environment variables:

| Provider | source |
| --- | --- |
| RunPod | `RUNPOD_API_KEY` env, else `~/.runpod/config.toml` (CLI-managed) |
| Modal | the `modal` SDK's own token store (`~/.modal.toml`) |
| Cerebrium | `~/.cerebrium/config.yaml` (CLI-managed); the adapter refreshes the session token and fetches the project inference key over HTTPS |
| http adapter | `${ENV_VAR}` substitution in configured headers — secrets stay in the environment |

Nothing writes credentials into traces, configs, or logs. If you find a code
path that does, treat it as a vulnerability (below).

## What the traces contain

JSONL traces record job ids, latencies, provider metrics, and cost figures.
Before publishing traces from your own runs, be aware they may include:

- provider job/run ids (harmless alone, but they identify your account's
  activity to the provider);
- account balance snapshots in `costs.jsonl` (delete or reduce to deltas);
- deployment identifiers (endpoint ids, app names) in configs.

`config/benchmark.yaml` in your working directory (real ids) overrides the
packaged example; keep the real one out of version control — the repo's
`.gitignore` already covers `config/`.

## Spending safety

- No subcommand submits a GPU job without an explicit `--go`.
- Every charge passes the projected-cost ledger's hard gates;
  `BudgetExceeded` stops submission at the configured operational stop.
- Cancels are confirmed to a terminal state; unconfirmed cancels are flagged
  `leaked` in traces so runaway jobs are visible, not silent.

## Reporting a vulnerability

Open a GitHub security advisory (preferred) or an issue with the label
`security` and no exploit details; we'll take it to a private channel.
Please include the affected file/function and reproduction steps.
