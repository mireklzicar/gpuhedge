"""RunPod Flash adapter — the cheap/fast RTX 4090 primary arm.

Targets the GPUHedge Flash ``moss4090`` endpoint built by ``deploy/runpod/``.
Submission goes through the Flash queue SDK (``Endpoint(id=...).run()`` +
``job.wait()`` — the ``/runsync`` path caps ~90 s and is unusable for cold
model loads). Status polling and cancellation use the RunPod REST v2 API so we
get real IN_QUEUE / IN_PROGRESS lifecycle stages and a genuine remote cancel.
"""

from __future__ import annotations

import asyncio
import base64
import pathlib
from typing import Any

import requests
import tomllib

from gpuhedge.backends.base import (
    Backend,
    BackendError,
    CancellationEvidence,
    CancellationReceipt,
    JobHandle,
    JobState,
    ProviderResult,
    now_ms,
)
from gpuhedge.config import Provider

_API_ROOT = "https://api.runpod.ai/v2"
_RUNPOD_CONFIG = pathlib.Path.home() / ".runpod" / "config.toml"


def load_runpod_api_key() -> str:
    """Read the key the ``runpod`` CLI stored (``~/.runpod/config.toml``)."""

    import os

    if os.environ.get("RUNPOD_API_KEY"):
        return os.environ["RUNPOD_API_KEY"]
    if not _RUNPOD_CONFIG.is_file():
        raise BackendError(
            "no RunPod API key: set RUNPOD_API_KEY or run `runpod config` "
            f"(expected {_RUNPOD_CONFIG})"
        )
    data = tomllib.loads(_RUNPOD_CONFIG.read_text())
    key = data.get("default", {}).get("api_key")
    if not key:
        raise BackendError(f"no [default].api_key in {_RUNPOD_CONFIG}")
    return key


def _status_to_state(status: str | None) -> JobState:
    return {
        "IN_QUEUE": JobState.QUEUED,
        "IN_PROGRESS": JobState.IN_PROGRESS,
        "COMPLETED": JobState.COMPLETED,
        "FAILED": JobState.FAILED,
        "CANCELLED": JobState.CANCELLED,
        "TIMED_OUT": JobState.FAILED,
    }.get(status or "", JobState.UNKNOWN)


class RunPodJob(JobHandle):
    def __init__(self, backend: RunPodBackend, job: Any, *, submit_ms: float) -> None:
        super().__init__(backend.key, submit_ms=submit_ms)
        self._backend = backend
        self._job = job
        data = getattr(job, "_data", None)
        self._id = str(data.get("id")) if data else getattr(job, "id", None)
        self._last_state = JobState.QUEUED

    def job_id(self) -> str | None:
        return self._id

    def _rest(self, verb: str, path: str, timeout: float = 15.0) -> dict[str, Any]:
        url = f"{_API_ROOT}/{self._backend.endpoint_id}/{path}"
        resp = requests.request(
            verb, url, headers=self._backend.headers, timeout=timeout
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}

    async def status(self) -> JobState:
        if self._id is None:
            return self._last_state
        data = await asyncio.to_thread(self._rest, "GET", f"status/{self._id}")
        state = _status_to_state(data.get("status"))
        if state is not JobState.UNKNOWN:
            self._last_state = state
        return state

    async def result(self, timeout_s: float) -> ProviderResult:
        try:
            await self._job.wait(timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - includes timeout
            state = JobState.TIMEOUT if "timeout" in str(exc).lower() else JobState.FAILED
            self._record(state.value.lower(), str(exc)[:200])
            return ProviderResult(
                provider=self.key, state=state,
                wall_s=(now_ms() - self.submit_ms) / 1000.0,
                events=self.events, error=str(exc)[:400],
            )

        status = self._job._data.get("status")
        wall_s = (now_ms() - self.submit_ms) / 1000.0
        if status != "COMPLETED":
            self._record("failed", str(status))
            return ProviderResult(
                provider=self.key, state=_status_to_state(status), wall_s=wall_s,
                events=self.events, error=str(getattr(self._job, "error", status))[:400],
            )

        output = self._job.output
        result = output.get("result", output) if isinstance(output, dict) else output
        if not isinstance(result, dict):
            self._record("failed", "non-dict output")
            return ProviderResult(
                provider=self.key, state=JobState.FAILED, wall_s=wall_s,
                events=self.events, error=str(result)[:400],
            )

        audio_b64 = result.get("audio_base64")
        audio = base64.b64decode(audio_b64) if audio_b64 else None
        self._record("result", f"{wall_s:.1f}s")
        metrics = dict(result.get("metrics", {}))
        # Provider-reported billed execution (ms) — the actual-cost anchor for
        # reconciling projected vs billed per request (price monitoring).
        exec_ms = self._job._data.get("executionTime")
        if exec_ms is not None:
            metrics["runpod_execution_ms"] = exec_ms
            metrics["runpod_billed_cost_usd"] = round(
                (exec_ms / 1000.0) * self._backend.provider.billed_rate_per_s, 6
            )
        delay_ms = self._job._data.get("delayTime")
        if delay_ms is not None:
            metrics["runpod_delay_ms"] = delay_ms  # queue wait before execution
        return ProviderResult(
            provider=self.key,
            state=JobState.COMPLETED,
            wall_s=wall_s,
            audio=audio,
            sample_rate=result.get("sample_rate"),
            provider_metrics=metrics,
            events=self.events,
        )

    async def cancel(self, *, reason: str = "lost the race") -> CancellationReceipt:
        cancel_sent = now_ms()
        was_running = self._last_state is JobState.IN_PROGRESS
        if self._id is None:
            # No handle to cancel through: no evidence, and conservatively a leak.
            return CancellationReceipt(
                provider=self.key, job_id=None, was_running=was_running,
                cancel_sent_ms=cancel_sent, note="no job id to cancel",
            )
        # POST /cancel/{job_id} — RunPod stops IN_PROGRESS jobs and drops queued ones.
        try:
            data = await asyncio.to_thread(self._rest, "POST", f"cancel/{self._id}")
            cancel_ack = now_ms()
        except Exception as exc:  # noqa: BLE001
            return CancellationReceipt(
                provider=self.key, job_id=self._id, was_running=was_running,
                cancel_sent_ms=cancel_sent, note=f"cancel call failed: {exc}",
            )
        # The POST response's status is only an acknowledgment — terminal_status
        # is set exclusively from a subsequent observed status poll.
        receipt = CancellationReceipt(
            provider=self.key, job_id=self._id, was_running=was_running,
            cancel_sent_ms=cancel_sent, cancel_ack_ms=cancel_ack,
            evidence=CancellationEvidence.PROVIDER_ACK.value,
            note=f"cancel ack status={data.get('status')!r}",
        )
        # Poll to a confirmed terminal state — an HTTP 200 is not proof (docs).
        await self._poll_terminal(receipt)
        return receipt

    async def _poll_terminal(self, receipt: CancellationReceipt, window_s: float = 20.0) -> None:
        deadline = now_ms() + window_s * 1000.0
        while now_ms() < deadline:
            data = await asyncio.to_thread(self._rest, "GET", f"status/{self._id}")
            state = _status_to_state(data.get("status"))
            if state is not JobState.UNKNOWN:
                self._last_state = state
            if state.terminal:
                receipt.terminal_ms = now_ms()
                receipt.terminal_status = state.value
                receipt.evidence = CancellationEvidence.CONFIRMED_TERMINAL.value
                receipt.confirmed_terminal = True
                receipt.leaked = False
                # Provider-reported billed execution on the terminal status is
                # the actual loser cost. Its ABSENCE means the job never
                # reached a worker (cancelled while queued) — execution and
                # cost are zero, not wall-clock-since-submit: RunPod bills
                # from worker start, and an unstarted job has no idle window
                # either.
                exec_ms = data.get("executionTime")
                if exec_ms is not None:
                    receipt.cancel_scope = "request"
                    receipt.execution_ms_before_cancel = float(exec_ms)
                    receipt.reconciled_cost_usd = round(
                        (float(exec_ms) / 1000.0) * self._backend.provider.billed_rate_per_s, 6
                    )
                    receipt.estimated_cost_usd = self._backend.provider.billed_cost(
                        float(exec_ms) / 1000.0
                    )
                    receipt.billing_stop_confirmed = True
                elif receipt.was_running:
                    # ran, but no executionTime surfaced yet: estimate from wall;
                    # billing stop is NOT confirmed for this receipt.
                    receipt.cancel_scope = "request"
                    billed_ms = receipt.terminal_ms - self.submit_ms
                    receipt.execution_ms_before_cancel = billed_ms
                    receipt.estimated_cost_usd = self._backend.provider.billed_cost(
                        billed_ms / 1000.0
                    )
                else:
                    receipt.cancel_scope = "queued_job"
                    receipt.execution_ms_before_cancel = 0.0
                    receipt.estimated_cost_usd = 0.0
                    receipt.reconciled_cost_usd = 0.0
                    receipt.billing_stop_confirmed = True
                    receipt.note = "cancelled in queue; no worker started"
                return
            await asyncio.sleep(1.0)
        receipt.leaked = True
        receipt.note = "not terminal within poll window"


class RunPodBackend(Backend):
    def __init__(self, provider: Provider, request: dict[str, Any]) -> None:
        super().__init__(provider, request)
        self.endpoint_id = provider.extra["endpoint_id"]
        self._api_key = load_runpod_api_key()
        self.headers = {"Authorization": f"Bearer {self._api_key}"}
        self._endpoint = None

    def _get_endpoint(self):
        if self._endpoint is None:
            from runpod_flash import Endpoint

            self._endpoint = Endpoint(id=self.endpoint_id)
        return self._endpoint

    def _payload(self) -> dict[str, Any]:
        # moss_worker maps {"data": {...}} -> function kwargs.
        return {"data": {
            "text": self.request["text"],
            "voice_id": self.request["voice_id"],
            "voice_dir": self.request["voice_dir"],
            "language": self.request.get("language", "English"),
        }}

    async def submit(self) -> JobHandle:
        ep = self._get_endpoint()
        submit_ms = now_ms()
        job = await ep.run(self._payload())
        return RunPodJob(self, job, submit_ms=submit_ms)

    async def purge_queue(self) -> dict[str, Any]:
        """Clear stale queued jobs that survive redeploys and hog the worker."""

        url = f"{_API_ROOT}/{self.endpoint_id}/purge-queue"
        resp = await asyncio.to_thread(
            requests.post, url, headers=self.headers, timeout=15
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def scale_to_zero(self) -> None:
        # 4090 workers scale down on their own after idle_timeout=60 s; the round
        # driver waits past that. We only purge any stale queued job here.
        try:
            await self.purge_queue()
        except Exception:  # noqa: BLE001 - best effort
            pass
