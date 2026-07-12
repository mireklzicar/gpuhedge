"""Provider adapters and the factory that builds one from config.

Adapters are imported lazily so that ``gpuhedge`` (and CI) can import the
package without every cloud SDK installed — ``pip install gpuhedge[runpod]``
etc. pulls only what a given provider needs.
"""

from __future__ import annotations

from typing import Any

from gpuhedge.backends.base import (
    Backend,
    BackendError,
    CancellationEvidence,
    CancellationReceipt,
    JobHandle,
    JobState,
    LifecycleEvent,
    NotDeployedError,
    ProviderResult,
)
from gpuhedge.config import Provider

_REGISTRY = {
    "runpod": ("gpuhedge.backends.runpod_backend", "RunPodBackend"),
    "modal": ("gpuhedge.backends.modal_backend", "ModalBackend"),
    "cerebrium": ("gpuhedge.backends.cerebrium_backend", "CerebriumBackend"),
    "sim": ("gpuhedge.backends.sim_backend", "SimBackend"),
    "http": ("gpuhedge.backends.http_backend", "HttpBackend"),
}


def build_backend(provider: Provider, request: dict[str, Any]) -> Backend:
    """Construct the adapter for the provider.

    The adapter is selected by ``extra.adapter`` when present (``sim``,
    ``http``, or any registry key), else by the provider key itself — so a
    config can declare arbitrarily-named providers backed by the generic
    adapters."""

    import importlib

    adapter = provider.extra.get("adapter", provider.key)
    if adapter not in _REGISTRY:
        raise BackendError(
            f"no adapter {adapter!r} for provider {provider.key!r} "
            f"(known: {sorted(_REGISTRY)})"
        )
    module_name, cls_name = _REGISTRY[adapter]
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)(provider, request)


__all__ = [
    "Backend",
    "BackendError",
    "NotDeployedError",
    "JobHandle",
    "JobState",
    "LifecycleEvent",
    "ProviderResult",
    "CancellationEvidence",
    "CancellationReceipt",
    "build_backend",
]
