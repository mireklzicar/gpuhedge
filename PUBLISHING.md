# Pre-publication checklist

This repo was developed against real provider accounts. Before making any
copy of it public (git remote, release bundle, zip sent for review), walk
this list — it is short and every step matters.

The real identifier -> placeholder map lives in the **gitignored**
`config/sanitize_identifiers.json`; the checks below read from it so that no
real value ever appears in a committed file (including this one — an earlier
draft of this checklist embedded the very identifiers it existed to scrub).

## 1. History

Pre-sanitization commits contain real endpoint ids, account balances in
`traces/costs.jsonl`, and an early README with account identities. **Squash
to a fresh root** (or `git init` a clean copy and commit once) before pushing
to a public remote. Do not rely on later commits having scrubbed earlier
ones — history is part of the release.

## 2. Traces (JSONL)

```bash
python scripts/sanitize_traces.py       # reads config/sanitize_identifiers.json
```

Then replace `traces/` with the sanitized copies and verify the numbers are
unchanged:

```bash
python -m gpuhedge replay traces/moss_rounds.jsonl
python benchmarks/2026-07-queue-cutover/analysis.py
```

The sanitizer remaps job ids (salted, unrecoverable), nulls absolute
balances (keeping spend deltas), and replaces deployment identifiers with
placeholders — latencies, receipts, and cost deltas are untouched.

## 3. Everything the trace sanitizer does NOT cover

The sanitizer handles JSONL only. Each of these needs its own pass:

- **`.env` files** — never committed (`.gitignore` has `.env`); verify none
  are tracked and use placeholders in every published example:
  `git ls-files | grep -F .env` must show only `*.env.example`.
- **Logs** (`traces/*.log`, driver logs) — contain local filesystem paths
  and raw ids; exclude from the repo and from any bundle.
- **Shell scripts** (`scripts/*.sh`) — check for hardcoded ids/paths.
- **YAML** — the packaged `src/gpuhedge/config/benchmark.yaml` must ship
  placeholders and `deployed: false`; the real `./config/` is gitignored
  (verify: `git check-ignore config/benchmark.yaml`).
- **Notebooks / generated reports / figures** — regenerate from sanitized
  inputs; check embedded output cells.
- **Stray runtime output** — e.g. `src/gpuhedge/traces/` (gitignored; must
  not be tracked).

## 4. Final sweep (files AND history)

```bash
python - <<'PY'
import json, subprocess, sys
ids = json.load(open("config/sanitize_identifiers.json"))
bad = False
for real in ids:
    # working tree
    if subprocess.run(["git", "grep", "-qF", real], capture_output=True).returncode == 0:
        print("LEAK in tree:", ids[real]); bad = True
    # full history
    if subprocess.run(["git", "log", "-S", real, "--oneline"],
                      capture_output=True, text=True).stdout.strip():
        print("LEAK in history:", ids[real]); bad = True
sys.exit(1 if bad else 0)
PY
make lint && make test
pip install -e ".[analysis]"
python benchmarks/2026-07-moss/analysis.py   # figures regenerate from committed data
gpuhedge demo --requests 6                   # the no-signup demo works from a clean clone
```

Run a general secret scanner (e.g. `gitleaks detect`) over files and history
as the last step.

## 5. Claims discipline (see docs/cost-accounting.md)

- Latency headline: counts alongside percentages ("0/36", not "zero misses").
- Cost claims name their model (active-compute vs billed vs account delta).
- The queue-cutover story cites the pre-registered validation — including
  that its p95 comparison is estimator-sensitive at n=20 — not the post-hoc
  replay.
- Quantile estimators are preregistered for new experiments; p95 is not a
  primary endpoint at n≈20.
- The cancellation capability matrix reflects what was actually exercised
  live per provider, with evidence levels, and the tagline never claims more
  than `confirmed_terminal` receipts support.
