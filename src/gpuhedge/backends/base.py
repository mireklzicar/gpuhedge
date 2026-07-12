"""Uniform provider abstraction: submit -> result / status / cancel, plus the
structured cancellation receipt that is GPUHedge's core differentiator.

Every adapter implements the same small surface so the benchmark controller and
the (future) production router treat RunPod, Modal, and Cerebrium identically.
Provider capability differences (e.g. fine-grained lifecycle events) are exposed
honestly via ``LifecycleEvent`` lists that may be coarse for some providers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from gpuhedge.config import Provider


class JobState(str, Enum):
    PENDING = "PENDING"
    QUEUED = "IN_QUEUE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"       # right-censored: exceeded the per-model cap
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMEOUT,
        }


def now_ms() -> float:
    """Monotonic milliseconds — safe for durations, never wall-clock arithmetic."""

    return time.monotonic() * 1000.0


@dataclass
class LifecycleEvent:
    """One observed transition, ms relative to submit (round-relative if shared)."""

    t_ms: float
    stage: str            # submitted|queued|in_progress|model_load|generating|result|...
    detail: str = ""


@dataclass
class ProviderResult:
    provider: str
    state: JobState
    wall_s: float                                  # submit -> valid result / terminal
    audio: bytes | None = None                     # decoded WAV bytes (winner only)
    sample_rate: int | None = None
    provider_metrics: dict[str, Any] = field(default_factory=dict)  # load_seconds, gpu, ...
    events: list[LifecycleEvent] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is JobState.COMPLETED and self.audio is not None


class CancellationEvidence(str, Enum):
    """How much proof the adapter obtained that the loser actually stopped.

    Ordered strongest to weakest. An adapter must EARN a level: acknowledgment
    of the cancel call and terminal-state confirmation are distinct events,
    and an unconfirmed cancellation never defaults to success
    (docs/cancellation-semantics.md).
    """

    CONFIRMED_TERMINAL = "confirmed_terminal"        # polled to a terminal state
    PROVIDER_ACK = "provider_ack"                    # cancel call acknowledged only
    REQUEST_CHANNEL_CLOSED = "request_channel_closed"  # in-flight request broke
    NO_EVIDENCE = "no_evidence"                      # nothing observable


@dataclass
class CancellationReceipt:
    """Audit record for a loser cancellation — the evidence trail that
    separates GPUHedge from an ``asyncio.wait(FIRST_COMPLETED)`` demo
    (docs/cancellation-semantics.md).

    Conservative by default: ``evidence`` starts at NO_EVIDENCE and ``leaked``
    starts True. Adapters flip them only when they observe proof."""

    provider: str
    job_id: str | None
    was_running: bool                       # cancelled IN_PROGRESS vs merely IN_QUEUE
    cancel_sent_ms: float
    cancel_ack_ms: float | None = None      # provider acknowledged the cancel call
    terminal_ms: float | None = None        # job reached a terminal state on poll
    terminal_status: str | None = None      # set ONLY from an observed terminal state
    last_gpu_activity_ms: float | None = None
    execution_ms_before_cancel: float | None = None
    estimated_cost_usd: float | None = None
    reconciled_cost_usd: float | None = None
    evidence: str = CancellationEvidence.NO_EVIDENCE.value
    cancel_scope: str = "unknown"           # queued_job | request | container | unknown
    confirmed_terminal: bool = False        # terminal state observed, not assumed
    billing_stop_confirmed: bool = False    # provider-reported final billing captured
    leaked: bool = True                     # True until stopping is evidenced
    note: str = ""

    def ack_latency_ms(self) -> float | None:
        if self.cancel_ack_ms is None:
            return None
        return self.cancel_ack_ms - self.cancel_sent_ms

    def cancel_to_terminal_ms(self) -> float | None:
        if self.terminal_ms is None:
            return None
        return self.terminal_ms - self.cancel_sent_ms


class BackendError(RuntimeError):
    pass


class NotDeployedError(BackendError):
    """Raised when a provider's endpoint has not been deployed yet
    (``deployed: false`` in the config — e.g. Cerebrium before Stage 1)."""


class JobHandle:
    """A submitted job on one provider. Adapters subclass and implement the
    async methods; base tracks submit time so wall/relative timings are uniform."""

    provider: str

    def __init__(self, provider: str, *, submit_ms: float) -> None:
        self.provider = provider
        self.key = provider          # adapters build ProviderResult(provider=self.key)
        self.submit_ms = submit_ms
        self.events: list[LifecycleEvent] = [LifecycleEvent(0.0, "submitted")]

    def _record(self, stage: str, detail: str = "") -> None:
        self.events.append(LifecycleEvent(now_ms() - self.submit_ms, stage, detail))

    def job_id(self) -> str | None:  # pragma: no cover - overridden
        return None

    async def result(self, timeout_s: float) -> ProviderResult:  # pragma: no cover
        raise NotImplementedError

    async def status(self) -> JobState:  # pragma: no cover
        raise NotImplementedError

    async def cancel(  # pragma: no cover
        self, *, reason: str = "lost the race"
    ) -> CancellationReceipt:
        raise NotImplementedError


class Backend:
    """Factory + provider-level operations (submit, force cold)."""

    key: str

    def __init__(self, provider: Provider, request: dict[str, Any]) -> None:
        self.provider = provider
        self.key = provider.key
        self.request = request

    def available(self) -> bool:
        return self.provider.deployed

    async def submit(self) -> JobHandle:  # pragma: no cover - overridden
        raise NotImplementedError

    async def scale_to_zero(self) -> None:
        """Best-effort force the endpoint into its normal cold state before a
        round. Default no-op; adapters override where the platform allows it."""

        return None
