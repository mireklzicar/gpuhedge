"""Produce publishable copies of the raw traces.

    python scripts/sanitize_traces.py [--traces traces/] [--out <dir>]
                                      [--identifiers <json>]

What it does (and why):
- remaps job/run ids to salted short hashes (they identify account activity);
- nulls absolute account balances, keeping spend-since-baseline deltas;
- replaces deployment identifiers (endpoint/project/app ids, account user
  ids) with stable placeholders everywhere, including inside error strings;
- leaves every latency, state, metric, receipt timing, and cost delta
  untouched — the analysis (`gpuhedge replay`, benchmarks/*/analysis.py)
  produces identical numbers from the sanitized copies.

The real identifier -> placeholder map lives in a GITIGNORED file (default
``config/sanitize_identifiers.json``) so this script never leaks the values
it exists to scrub:

    {"my-real-endpoint-id": "ENDPOINT_A", "p-myproject": "PROJECT_A"}

The salt is random per run and NOT stored: hashes are stable within one
sanitizer run (so cross-file joins still work) but cannot be reversed or
correlated with a later re-run.

Scope note (PUBLISHING.md covers the rest): this handles JSONL traces only.
`.env` files, logs, shell scripts, YAML configs, notebooks, generated
reports, and git history need their own pass before publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

DEFAULT_IDENTIFIERS = Path("config/sanitize_identifiers.json")

# Deployment identifiers -> stable placeholders; loaded from the gitignored
# identifiers file in main().
IDENTIFIER_MAP: dict[str, str] = {}

_SALT = secrets.token_hex(8)


def _hash_id(value: str) -> str:
    return "job-" + hashlib.sha1(f"{_SALT}:{value}".encode()).hexdigest()[:12]


def _scrub_str(s: str) -> str:
    for real, placeholder in IDENTIFIER_MAP.items():
        s = s.replace(real, placeholder)
    return s


def _walk(node: Any, *, key: str | None = None) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, key=k) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, key=key) for v in node]
    if isinstance(node, str):
        if key in ("job_id", "run_id") and node:
            return _hash_id(node)
        return _scrub_str(node)
    return node


def _sanitize_record(rec: dict[str, Any]) -> dict[str, Any]:
    rec = _walk(rec)
    if rec.get("kind") == "cost_snapshot":
        for reading in rec.get("readings", {}).values():
            if reading.get("kind") == "balance":
                # absolute balances identify the account; deltas carry the data
                reading["value_usd"] = None
                reading["detail"] = _scrub_str(
                    "balance withheld; see actual_spend_since_baseline"
                )
    return rec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", default="traces")
    parser.add_argument("--out", default="benchmarks/2026-07-moss/sanitized-traces")
    parser.add_argument("--identifiers", default=str(DEFAULT_IDENTIFIERS),
                        help="gitignored JSON mapping real ids -> placeholders")
    args = parser.parse_args()

    ids_path = Path(args.identifiers)
    if ids_path.is_file():
        IDENTIFIER_MAP.update(json.loads(ids_path.read_text()))
    else:
        raise SystemExit(
            f"{ids_path} not found — create it (gitignored) with your real "
            'identifiers, e.g. {"my-endpoint-id": "ENDPOINT_A"}. Refusing to '
            "sanitize without an identifier map."
        )

    src = Path(args.traces)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*.jsonl")):
        n = 0
        with (out / path.name).open("w") as fh:
            for line in path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fh.write(json.dumps(_sanitize_record(rec)) + "\n")
                n += 1
        print(f"  {path.name}: {n} records -> {out / path.name}")

    leftovers = []
    pattern = re.compile("|".join(re.escape(k) for k in IDENTIFIER_MAP))
    for path in sorted(out.glob("*.jsonl")):
        if pattern.search(path.read_text()):
            leftovers.append(path.name)
    if leftovers:
        raise SystemExit(f"identifiers survived sanitization in: {leftovers}")
    print("no known identifiers remain in the sanitized copies")


if __name__ == "__main__":
    main()
