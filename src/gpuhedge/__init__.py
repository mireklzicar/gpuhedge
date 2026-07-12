"""GPUHedge — speculative execution across serverless GPU clouds.

First valid result wins. Loser cancellation is audited.

The public surface is intentionally small; the benchmark harness lives under
``gpuhedge.benchmark`` and provider adapters under ``gpuhedge.backends``.
"""

from __future__ import annotations

from gpuhedge.config import BenchmarkConfig, default_config_path, load_config


def __getattr__(name: str):
    # Lazy: Router pulls in telemetry/policy modules; keep bare import light.
    if name in ("Router", "RouterOutcome"):
        from gpuhedge import router

        return getattr(router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BenchmarkConfig",
    "load_config",
    "default_config_path",
    "Router",
    "RouterOutcome",
]
