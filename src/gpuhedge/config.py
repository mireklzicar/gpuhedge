"""Typed access to ``config/benchmark.yaml`` — the single source of truth for the
three-provider MOSS-TTS cold-start plan (benchmarks/2026-07-moss/methodology.md).

Nothing in the codebase hard-codes rates, budget gates, endpoint ids, or the
request; it all comes from here so the plan stays auditable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PACKAGED_CONFIG = Path(__file__).resolve().parent / "config" / "benchmark.yaml"


def default_config_path() -> Path:
    """Resolve the config to use: ``./config/benchmark.yaml`` if present in the
    working directory (an operator's edited copy), else the packaged default."""

    local = Path.cwd() / "config" / "benchmark.yaml"
    if local.is_file():
        return local
    return _PACKAGED_CONFIG


@dataclass(frozen=True)
class Provider:
    key: str
    role: str
    gpu: str
    billed_rate_per_s: float
    gpu_rate_per_s: float
    region: str
    deployed: bool
    idle_billed_seconds: float = 0.0
    tokenizer_device: str = "gpu"
    gpu_vram_gb: int | None = None
    # provider-specific handles (endpoint_id / app / cls / method / function ...)
    extra: dict[str, Any] = field(default_factory=dict)

    def billed_cost(self, billed_seconds: float, *, include_idle: bool = True) -> float:
        seconds = billed_seconds + (self.idle_billed_seconds if include_idle else 0.0)
        return round(seconds * self.billed_rate_per_s, 6)


@dataclass(frozen=True)
class Budget:
    currency: str
    operational_stop: float
    absolute_ceiling: float
    gates: dict[str, float]
    stage_allocation: dict[str, float]


@dataclass(frozen=True)
class BenchmarkConfig:
    path: Path
    raw: dict[str, Any]
    model: str
    request: dict[str, Any]
    providers: dict[str, Provider]
    policy: dict[str, Any]
    slo: dict[str, Any]
    timeouts_s: dict[str, float]
    stages: dict[str, Any]
    budget: Budget
    qualification: dict[str, Any]
    output: dict[str, Any]

    # ----------------------------------------------------------- convenience
    def provider(self, key: str) -> Provider:
        try:
            return self.providers[key]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"unknown provider {key!r}; have {sorted(self.providers)}") from exc

    def trace_dir(self) -> Path:
        """Traces/ledger live under the operator's working directory (or
        ``GPUHEDGE_TRACE_DIR``), never inside the installed package."""

        import os

        env = os.environ.get("GPUHEDGE_TRACE_DIR")
        if env:
            return Path(env).resolve()
        return (Path.cwd() / self.output.get("trace_dir", "traces")).resolve()

    def moss_timeout_s(self) -> float:
        return float(self.timeouts_s["moss"])


_KNOWN_PROVIDER_FIELDS = {
    "role",
    "gpu",
    "billed_rate_per_s",
    "gpu_rate_per_s",
    "region",
    "deployed",
    "idle_billed_seconds",
    "tokenizer_device",
    "gpu_vram_gb",
}


def _build_provider(key: str, spec: dict[str, Any]) -> Provider:
    extra = {k: v for k, v in spec.items() if k not in _KNOWN_PROVIDER_FIELDS}
    return Provider(
        key=key,
        role=spec["role"],
        gpu=spec["gpu"],
        billed_rate_per_s=float(spec["billed_rate_per_s"]),
        gpu_rate_per_s=float(spec["gpu_rate_per_s"]),
        region=spec["region"],
        deployed=bool(spec.get("deployed", False)),
        idle_billed_seconds=float(spec.get("idle_billed_seconds", 0.0)),
        tokenizer_device=spec.get("tokenizer_device", "gpu"),
        gpu_vram_gb=spec.get("gpu_vram_gb"),
        extra=extra,
    )


def load_config(path: str | Path | None = None) -> BenchmarkConfig:
    """Load and validate the benchmark config."""

    cfg_path = Path(path).resolve() if path else default_config_path()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    providers = {key: _build_provider(key, spec) for key, spec in raw["providers"].items()}
    budget_raw = raw["budget"]
    budget = Budget(
        currency=budget_raw.get("currency", "USD"),
        operational_stop=float(budget_raw["operational_stop"]),
        absolute_ceiling=float(budget_raw["absolute_ceiling"]),
        gates={k: float(v) for k, v in budget_raw["gates"].items()},
        stage_allocation={k: float(v) for k, v in budget_raw.get("stage_allocation", {}).items()},
    )

    return BenchmarkConfig(
        path=cfg_path,
        raw=raw,
        model=raw["model"],
        request=raw["request"],
        providers=providers,
        policy=raw["policy"],
        slo=raw["slo"],
        timeouts_s={k: float(v) for k, v in raw["timeouts_s"].items()},
        stages=raw["stages"],
        budget=budget,
        qualification=raw["qualification"],
        output=raw.get("output", {}),
    )
