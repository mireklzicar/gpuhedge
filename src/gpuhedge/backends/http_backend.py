"""Generic asynchronous HTTP job adapter — describe any submit/status/result/
cancel service in YAML instead of writing a Python adapter.

    providers:
      my_gpu_service:
        role: hedge
        gpu: L40S
        region: us-east-1
        gpu_rate_per_s: 0.000542
        billed_rate_per_s: 0.000542
        deployed: true
        adapter: http
        http:
          headers:                       # ${ENV_VAR} substitution
            Authorization: "Bearer ${MY_SERVICE_TOKEN}"
          submit:
            method: POST
            url: https://example.com/jobs
            body:                        # merged with the request payload
              input: "{request}"         # literal "{request}" -> request dict
            job_id_path: id              # dotted path into the JSON response
          status:
            method: GET
            url: https://example.com/jobs/{job_id}
            state_path: status
            state_map:                   # service value -> gpuhedge JobState
              queued: QUEUED
              running: IN_PROGRESS
              succeeded: COMPLETED
              failed: FAILED
              cancelled: CANCELLED
          result:
            method: GET
            url: https://example.com/jobs/{job_id}
            audio_b64_path: output.audio_base64
            sample_rate_path: output.sample_rate
            metrics_path: output.metrics
            poll_interval_s: 1.0
          cancel:
            method: DELETE
            url: https://example.com/jobs/{job_id}

The adapter polls ``status`` until a terminal state, then fetches ``result``.
Cancellation POSTs/DELETEs and then polls status to a confirmed terminal state
— an HTTP 200 on the cancel call is not accepted as proof.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import Any

import requests

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

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _substitute_env(value: str) -> str:
    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise BackendError(f"http adapter: environment variable {name} not set")
        return os.environ[name]

    return _ENV_PATTERN.sub(repl, value)


def dig(payload: Any, path: str | None) -> Any:
    """Follow a dotted path ('a.b.0.c') into parsed JSON; None if absent."""

    if path in (None, "", "$"):
        return payload
    node = payload
    for part in str(path).removeprefix("$.").split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            idx = int(part)
            node = node[idx] if idx < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


class HttpJob(JobHandle):
    def __init__(self, backend: HttpBackend, job_id: str | None, *,
                 submit_ms: float) -> None:
        super().__init__(backend.key, submit_ms=submit_ms)
        self._backend = backend
        self._id = job_id
        self._last_state = JobState.QUEUED

    def job_id(self) -> str | None:
        return self._id

    async def status(self) -> JobState:
        spec = self._backend.spec.get("status")
        if not spec or self._id is None:
            return self._last_state
        try:
            payload = await self._backend.call(spec, job_id=self._id)
        except Exception:  # noqa: BLE001 - transient poll failure
            return self._last_state
        state = self._backend.map_state(dig(payload, spec.get("state_path")))
        if state is not JobState.UNKNOWN:
            self._last_state = state
        return state

    async def result(self, timeout_s: float) -> ProviderResult:
        deadline = self.submit_ms + timeout_s * 1000.0
        spec = self._backend.spec.get("result") or {}
        interval = float(spec.get("poll_interval_s", 1.0))
        while now_ms() < deadline:
            state = await self.status()
            if state.terminal:
                break
            await asyncio.sleep(interval)
        wall_s = (now_ms() - self.submit_ms) / 1000.0
        state = self._last_state
        if not state.terminal:
            self._record("timeout", f"{wall_s:.1f}s")
            return ProviderResult(provider=self.key, state=JobState.TIMEOUT,
                                  wall_s=wall_s, events=self.events,
                                  error="cap reached before a terminal state")
        if state is not JobState.COMPLETED:
            self._record(state.value.lower(), "")
            return ProviderResult(provider=self.key, state=state, wall_s=wall_s,
                                  events=self.events, error=state.value)
        try:
            payload = await self._backend.call(spec, job_id=self._id)
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(provider=self.key, state=JobState.FAILED,
                                  wall_s=wall_s, events=self.events,
                                  error=f"result fetch failed: {exc}"[:400])
        audio_b64 = dig(payload, spec.get("audio_b64_path"))
        self._record("result", f"{wall_s:.1f}s")
        return ProviderResult(
            provider=self.key, state=JobState.COMPLETED, wall_s=wall_s,
            audio=base64.b64decode(audio_b64) if audio_b64 else None,
            sample_rate=dig(payload, spec.get("sample_rate_path")),
            provider_metrics=dig(payload, spec.get("metrics_path")) or {},
            events=self.events,
        )

    async def cancel(self, *, reason: str = "lost the race") -> CancellationReceipt:
        cancel_sent = now_ms()
        was_running = self._last_state is JobState.IN_PROGRESS
        spec = self._backend.spec.get("cancel")
        if not spec or self._id is None:
            return CancellationReceipt(
                provider=self.key, job_id=self._id, was_running=was_running,
                cancel_sent_ms=cancel_sent, note="no cancel endpoint configured",
            )
        try:
            await self._backend.call(spec, job_id=self._id)
            cancel_ack = now_ms()
        except Exception as exc:  # noqa: BLE001
            return CancellationReceipt(
                provider=self.key, job_id=self._id, was_running=was_running,
                cancel_sent_ms=cancel_sent, note=f"cancel call failed: {exc}",
            )
        receipt = CancellationReceipt(
            provider=self.key, job_id=self._id, was_running=was_running,
            cancel_sent_ms=cancel_sent, cancel_ack_ms=cancel_ack,
            evidence=CancellationEvidence.PROVIDER_ACK.value,
        )
        # HTTP 200 is not terminal proof: poll status to a confirmed stop.
        deadline = now_ms() + 20_000.0
        while now_ms() < deadline:
            state = await self.status()
            if state.terminal:
                receipt.terminal_ms = now_ms()
                receipt.terminal_status = state.value
                receipt.evidence = CancellationEvidence.CONFIRMED_TERMINAL.value
                receipt.confirmed_terminal = True
                receipt.cancel_scope = "request" if was_running else "queued_job"
                receipt.leaked = False
                billed_s = (receipt.terminal_ms - self.submit_ms) / 1000.0
                receipt.execution_ms_before_cancel = billed_s * 1000.0
                receipt.estimated_cost_usd = self._backend.provider.billed_cost(
                    billed_s, include_idle=False
                )
                return receipt
            await asyncio.sleep(1.0)
        receipt.leaked = True
        receipt.note = "not terminal within poll window"
        return receipt


class HttpBackend(Backend):
    def __init__(self, provider: Provider, request: dict[str, Any]) -> None:
        super().__init__(provider, request)
        self.spec = dict(provider.extra.get("http", {}))
        if "submit" not in self.spec:
            raise BackendError(f"provider {provider.key}: http adapter needs "
                               "an http.submit spec")
        self._default_state_map = {
            "QUEUED": JobState.QUEUED, "IN_QUEUE": JobState.QUEUED,
            "PENDING": JobState.QUEUED,
            "RUNNING": JobState.IN_PROGRESS, "IN_PROGRESS": JobState.IN_PROGRESS,
            "PROCESSING": JobState.IN_PROGRESS,
            "COMPLETED": JobState.COMPLETED, "SUCCEEDED": JobState.COMPLETED,
            "SUCCESS": JobState.COMPLETED,
            "FAILED": JobState.FAILED, "ERROR": JobState.FAILED,
            "CANCELLED": JobState.CANCELLED, "CANCELED": JobState.CANCELLED,
        }

    def headers(self) -> dict[str, str]:
        return {
            k: _substitute_env(str(v))
            for k, v in (self.spec.get("headers") or {}).items()
        }

    def map_state(self, value: Any) -> JobState:
        state_map = {
            **self._default_state_map,
            **{str(k).upper(): JobState[str(v).upper()]
               for k, v in (self.spec.get("status", {}).get("state_map") or {}).items()},
        }
        return state_map.get(str(value).upper(), JobState.UNKNOWN)

    def _body(self, spec: dict[str, Any]) -> Any:
        body = spec.get("body")
        if body is None:
            return dict(self.request)

        def render(node: Any) -> Any:
            if node == "{request}":
                return dict(self.request)
            if isinstance(node, dict):
                return {k: render(v) for k, v in node.items()}
            if isinstance(node, list):
                return [render(v) for v in node]
            return node

        return render(body)

    async def call(self, spec: dict[str, Any], *, job_id: str | None = None,
                   timeout: float = 30.0) -> Any:
        url = _substitute_env(str(spec["url"]))
        if job_id is not None:
            url = url.replace("{job_id}", job_id)
        method = str(spec.get("method", "GET")).upper()
        kwargs: dict[str, Any] = {"headers": self.headers(), "timeout": timeout}
        if method in ("POST", "PUT", "PATCH"):
            kwargs["json"] = self._body(spec)

        def _do() -> Any:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {}

        return await asyncio.to_thread(_do)

    async def submit(self) -> JobHandle:
        spec = self.spec["submit"]
        submit_ms = now_ms()
        payload = await self.call(spec)
        job_id = dig(payload, spec.get("job_id_path", "id"))
        return HttpJob(self, str(job_id) if job_id is not None else None,
                       submit_ms=submit_ms)

    async def scale_to_zero(self) -> None:
        return None
