"""Modal adapter — the reliable 48 GB L40S hedge arm.

Targets the GPUHedge Modal app (class ``MossTTS``, method ``tts``) built by
``deploy/modal/app.py``. Submission uses ``.spawn`` to get a server-side
``FunctionCall``, whose ``cancel(terminate_containers=True)`` is the
provider-native remote stop (Modal FunctionCall API, docs/architecture.md).

Limitation recorded honestly: a Modal ``FunctionCall`` does not surface
IN_QUEUE vs IN_PROGRESS stages the way RunPod/fal do, so lifecycle events here
are coarse (submitted / result). The container-terminated audit for the
cancellation receipt is completed at the app level in Stage 1 qualification.
"""

from __future__ import annotations

import asyncio
from typing import Any

from gpuhedge.backends.base import (
    Backend,
    CancellationEvidence,
    CancellationReceipt,
    JobHandle,
    JobState,
    ProviderResult,
    now_ms,
)
from gpuhedge.config import Provider


class ModalJob(JobHandle):
    def __init__(self, backend: ModalBackend, call: Any, *, submit_ms: float) -> None:
        super().__init__(backend.key, submit_ms=submit_ms)
        self._backend = backend
        self._call = call
        self._id = getattr(call, "object_id", None)
        self._done = False

    def job_id(self) -> str | None:
        return self._id

    async def status(self) -> JobState:
        if self._done:
            return JobState.COMPLETED
        try:
            # Non-blocking peek: ready -> returns; not ready -> TimeoutError.
            await asyncio.to_thread(self._call.get, 0)
            self._done = True
            return JobState.COMPLETED
        except Exception:  # noqa: BLE001 - TimeoutError while still running
            return JobState.IN_PROGRESS

    async def result(self, timeout_s: float) -> ProviderResult:
        try:
            payload = await asyncio.to_thread(self._call.get, timeout_s)
        except Exception as exc:  # noqa: BLE001 - includes TimeoutError
            state = JobState.TIMEOUT if "timeout" in type(exc).__name__.lower() else JobState.FAILED
            self._record(state.value.lower(), str(exc)[:200])
            return ProviderResult(
                provider=self.key, state=state,
                wall_s=(now_ms() - self.submit_ms) / 1000.0,
                events=self.events, error=str(exc)[:400],
            )
        self._done = True
        wall_s = (now_ms() - self.submit_ms) / 1000.0
        if not isinstance(payload, dict) or "audio" not in payload:
            self._record("failed", "unexpected payload")
            return ProviderResult(
                provider=self.key, state=JobState.FAILED, wall_s=wall_s,
                events=self.events, error=str(payload)[:400],
            )
        self._record("result", f"{wall_s:.1f}s")
        return ProviderResult(
            provider=self.key,
            state=JobState.COMPLETED,
            wall_s=wall_s,
            audio=payload["audio"],
            sample_rate=payload.get("sample_rate"),
            provider_metrics=payload.get("metrics", {}),
            events=self.events,
        )

    async def cancel(self, *, reason: str = "lost the race") -> CancellationReceipt:
        cancel_sent = now_ms()
        # terminate_containers=True would stop the GPU container (and billing),
        # not just the input — but Modal's current API rejects it
        # ("FunctionCallCancel request ... terminate_containers must be false",
        # observed live 2026-07-12), so _terminate falls back to a plain
        # cancel. The input stops; the container idles until its scaledown
        # window. Cancellation on Modal is therefore INPUT-granular in
        # practice; docs/provider-capabilities.md records this honestly.
        try:
            mode = await asyncio.to_thread(self._terminate)
            cancel_ack = now_ms()
        except Exception as exc:  # noqa: BLE001
            # A failed cancel means the job is still running: that is a leak.
            return CancellationReceipt(
                provider=self.key, job_id=self._id, was_running=not self._done,
                cancel_sent_ms=cancel_sent,
                note=f"cancel failed: {exc}",
            )
        # Modal's SDK acknowledges the input-cancel but exposes no pollable
        # terminal state for the call, and the container idles on to its
        # scaledown window. That is PROVIDER_ACK evidence with request scope —
        # never a confirmed terminal state, and never a confirmed billing stop.
        billed_ms = cancel_ack - self.submit_ms
        return CancellationReceipt(
            provider=self.key, job_id=self._id, was_running=not self._done,
            cancel_sent_ms=cancel_sent, cancel_ack_ms=cancel_ack,
            evidence=CancellationEvidence.PROVIDER_ACK.value,
            cancel_scope="request",
            leaked=False,
            execution_ms_before_cancel=billed_ms,
            estimated_cost_usd=self._backend.provider.billed_cost(billed_ms / 1000.0),
            note=f"cancel mode: {mode}; input-granular — the container idles to "
                 "its scaledown window and that idle tail keeps billing",
        )

    def _terminate(self) -> str:
        """Cancel the call; report which cancellation mode actually worked."""

        try:
            self._call.cancel(terminate_containers=True)
            return "terminate_containers=True"
        except Exception:  # noqa: BLE001 - API rejects the kwarg (2026-07) or
            pass           # older SDK lacks it; fall through to plain cancel
        self._call.cancel()
        return "input-cancel (terminate_containers rejected by API)"


class ModalBackend(Backend):
    def __init__(self, provider: Provider, request: dict[str, Any]) -> None:
        super().__init__(provider, request)
        self.app = provider.extra["app"]
        self.cls_name = provider.extra["cls"]
        self.method = provider.extra.get("method", "tts")
        self._svc = None

    def _service(self):
        if self._svc is None:
            import modal

            cls = modal.Cls.from_name(self.app, self.cls_name)
            self._svc = cls()
        return self._svc

    def _kwargs(self) -> dict[str, Any]:
        return {
            "text": self.request["text"],
            "voice_id": self.request["voice_id"],
            "voice_dir": self.request["voice_dir"],
            "language": self.request.get("language", "English"),
            "reference": self.request.get("reference", "cloned"),
        }

    async def submit(self) -> JobHandle:
        svc = self._service()
        method = getattr(svc, self.method)
        submit_ms = now_ms()
        call = await asyncio.to_thread(lambda: method.spawn(**self._kwargs()))
        return ModalJob(self, call, submit_ms=submit_ms)

    async def scale_to_zero(self) -> None:
        """No-op: the benchmark deploy uses ``scaledown_window=20`` s, so the
        controller's inter-round wait (>= 130 s) guarantees the container is
        gone without an explicit ``modal container stop``."""

        return None
