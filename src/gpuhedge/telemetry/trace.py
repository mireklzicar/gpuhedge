"""Append-only JSONL trace writer.

Every provider event and round outcome is saved so policies can be replayed
offline without spending GPU money again (docs/architecture.md §5). One line
per record; the file is the ground truth for the survival/Pareto analysis.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceWriter:
    """Line-buffered JSONL sink. Safe to keep open for a whole stage."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", utc_now_iso())
        self._fh.write(json.dumps(record, default=_json_default) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _json_default(obj: Any) -> Any:
    # dataclasses and enums show up in records; degrade gracefully.
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


def read_traces(path: str | Path) -> list[dict[str, Any]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
