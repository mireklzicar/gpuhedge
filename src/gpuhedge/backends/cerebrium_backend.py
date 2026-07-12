"""Cerebrium adapter — hardware-matched 48 GB L40S control against Modal.

API behaviour verified LIVE on 2026-07-11 (docs + observed responses):

- **Invoke**: ``POST https://api.cerebrium.ai/v4/{project_id}/{app}/{function}``
  with the project's *inference* JWT (``source: cerebrium_jwt`` from
  ``GET rest.cerebrium.ai/v2/projects/{pid}/api-keys``). The CLI session token
  is rejected by the gateway (observed 401).
- **Sync responses** carry ``{"run_id", "result", "run_time_ms"}`` — the
  function's return value arrives on the response body.
- **Async is broken for our use**: ``?async=true`` returns a ``run_id``, but the
  run-detail status stays ``"processing"`` with ``runtimeMs: 0`` forever, even
  after the app logs success (observed 25+ min). No result payload either. So
  this adapter is **sync-first**: submit runs the POST in a thread; the run_id
  for cancellation is discovered from the runs list
  (``GET /v2/projects/{pid}/apps/{app_id}/runs``, newest matching call).
- **Cancel**: ``DELETE .../runs/{run_id}`` -> 200. Because run status does not
  reconcile, the receipt's terminal proof is the in-flight HTTP request
  breaking (connection close / error response) — an observable, honest proxy
  for "the handler stopped". Billing verification stays a Stage 1 criterion.
- Single-run GET wraps payloads as ``{"item": {...}}``; observed status values:
  ``processing`` / ``success`` / ``failure`` / ``cancelled``.
"""

from __future__ import annotations

import asyncio
import base64
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import yaml

from gpuhedge.backends.base import (
    Backend,
    BackendError,
    CancellationEvidence,
    CancellationReceipt,
    JobHandle,
    JobState,
    NotDeployedError,
    ProviderResult,
    now_ms,
)
from gpuhedge.config import Provider

_REST = "https://rest.cerebrium.ai/v2"
_CEREBRIUM_CONFIG = pathlib.Path.home() / ".cerebrium" / "config.yaml"
# Cerebrium sessions are AWS Cognito (eu-west-1); access tokens live ~4h. The
# CLI stores a refresh token we can exchange headlessly — without this, long
# benchmarks die mid-run with 403s (observed at Stage 2 block 3).
_COGNITO_URL = "https://cognito-idp.eu-west-1.amazonaws.com/"
_COGNITO_CLIENT_ID = "2om0uempl69t4c6fc70ujstsuk"


def _jwt_expires_within(token: str, seconds: float) -> bool:
    import base64
    import json as _json
    import time

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        return exp < time.time() + seconds
    except Exception:  # noqa: BLE001 - opaque token: assume valid
        return False


def refresh_cerebrium_token() -> str:
    """Exchange the stored Cognito refresh token for a fresh access token and
    persist it back to the CLI config."""

    data = yaml.safe_load(_CEREBRIUM_CONFIG.read_text())
    refresh = data.get("refreshtoken")
    if not refresh:
        raise BackendError(
            "cerebrium session expired and no refreshtoken stored — "
            "run `cerebrium login`"
        )
    resp = requests.post(
        _COGNITO_URL,
        headers={"Content-Type": "application/x-amz-json-1.1",
                 "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"},
        json={"AuthFlow": "REFRESH_TOKEN_AUTH",
              "ClientId": _COGNITO_CLIENT_ID,
              "AuthParameters": {"REFRESH_TOKEN": refresh}},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["AuthenticationResult"]["AccessToken"]
    data["accesstoken"] = token
    _CEREBRIUM_CONFIG.write_text(yaml.safe_dump(data))
    return token


def load_cerebrium_token() -> str:
    """Platform-session JWT (rest.cerebrium.ai only), refreshed if near expiry."""

    import os

    if os.environ.get("CEREBRIUM_TOKEN"):
        return os.environ["CEREBRIUM_TOKEN"]
    if not _CEREBRIUM_CONFIG.is_file():
        raise BackendError(
            "no Cerebrium token: run `cerebrium login` "
            f"(expected {_CEREBRIUM_CONFIG})"
        )
    data = yaml.safe_load(_CEREBRIUM_CONFIG.read_text())
    token = data.get("accesstoken") or data.get("accessToken")
    if not token:
        raise BackendError(f"no accesstoken in {_CEREBRIUM_CONFIG}")
    if _jwt_expires_within(token, 120):
        token = refresh_cerebrium_token()
    return token


def load_cerebrium_inference_key(project_id: str) -> str:
    """Project inference JWT (the session token 401s on the inference gateway)."""

    import os

    if os.environ.get("CEREBRIUM_INFERENCE_KEY"):
        return os.environ["CEREBRIUM_INFERENCE_KEY"]
    resp = requests.get(
        f"{_REST}/projects/{project_id}/api-keys",
        headers={"Authorization": f"Bearer {load_cerebrium_token()}"},
        timeout=30,
    )
    if resp.status_code in (401, 403):
        resp = requests.get(
            f"{_REST}/projects/{project_id}/api-keys",
            headers={"Authorization": f"Bearer {refresh_cerebrium_token()}"},
            timeout=30,
        )
    resp.raise_for_status()
    for key in resp.json():
        if key.get("source") == "cerebrium_jwt" and key.get("apiKey"):
            return key["apiKey"]
    raise BackendError(
        f"no cerebrium_jwt api-key found for project {project_id}; "
        "create one on the dashboard API Keys page"
    )


class CerebriumJob(JobHandle):
    """One sync invocation running in a worker thread.

    ``status()`` reflects the request channel (the platform's run status never
    reconciles); ``cancel()`` discovers the run id from the runs list and
    DELETEs it, then waits for the in-flight request to break."""

    def __init__(self, backend: CerebriumBackend, task: asyncio.Task, *, submit_ms: float,
                 submitted_at: datetime) -> None:
        super().__init__(backend.key, submit_ms=submit_ms)
        self._backend = backend
        self._task = task
        self._submitted_at = submitted_at
        self._id: str | None = None

    def job_id(self) -> str | None:
        return self._id

    async def _discover_run_id(self, attempts: int = 5) -> str | None:
        """Find our run in the app's runs list (newest matching function call
        created at/after our submit time). max_replicas=1 and sequential
        benchmark submits keep this unambiguous."""

        if self._id is not None:
            return self._id
        floor = self._submitted_at - timedelta(seconds=5)
        for _ in range(attempts):
            try:
                data = await asyncio.to_thread(
                    self._backend.rest_app, "GET", "runs"
                )
                runs = data if isinstance(data, list) else data.get("items", [])
                for run in runs:  # list is newest-first
                    if run.get("functionName") != self._backend.function:
                        continue
                    if run.get("id") in self._backend.claimed_run_ids:
                        continue
                    created = _parse_ts(run.get("createdAt", ""))
                    if created and created >= floor:
                        self._id = run["id"]
                        self._backend.claimed_run_ids.add(self._id)
                        return self._id
            except Exception:  # noqa: BLE001 - retry
                pass
            await asyncio.sleep(1.0)
        return None

    async def status(self) -> JobState:
        if not self._task.done():
            return JobState.IN_PROGRESS
        if self._task.cancelled():
            return JobState.CANCELLED
        if self._task.exception() is not None:
            return JobState.FAILED
        return JobState.COMPLETED

    async def result(self, timeout_s: float) -> ProviderResult:
        try:
            payload = await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_s)
        except TimeoutError:
            # Right-censored at the cap: best-effort remote stop so the spending
            # controls hold (do not keep paying past the cap).
            wall_s = (now_ms() - self.submit_ms) / 1000.0
            self._record("timeout", f"{wall_s:.1f}s; sending cancel")
            try:
                await asyncio.wait_for(self.cancel(reason="right-censored at cap"), 40)
            except Exception:  # noqa: BLE001
                pass
            return ProviderResult(
                provider=self.key, state=JobState.TIMEOUT, wall_s=wall_s,
                events=self.events, error="right-censored at cap",
            )
        except Exception as exc:  # noqa: BLE001 - HTTP/connection errors
            wall_s = (now_ms() - self.submit_ms) / 1000.0
            self._record("failed", str(exc)[:200])
            return ProviderResult(
                provider=self.key, state=JobState.FAILED, wall_s=wall_s,
                events=self.events, error=str(exc)[:400],
            )

        wall_s = (now_ms() - self.submit_ms) / 1000.0
        self._id = payload.get("run_id", self._id)
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or "audio_base64" not in result:
            self._record("failed", "no audio in response")
            return ProviderResult(
                provider=self.key, state=JobState.FAILED, wall_s=wall_s,
                events=self.events, error=str(payload)[:400],
            )
        self._record("result", f"{wall_s:.1f}s")
        metrics = dict(result.get("metrics", {}))
        if "run_time_ms" in payload:
            # Provider-reported handler runtime — the actual-cost anchor.
            metrics["cerebrium_run_time_ms"] = payload["run_time_ms"]
            metrics["cerebrium_billed_cost_usd"] = round(
                (float(payload["run_time_ms"]) / 1000.0)
                * self._backend.provider.billed_rate_per_s, 6,
            )
        return ProviderResult(
            provider=self.key, state=JobState.COMPLETED, wall_s=wall_s,
            audio=base64.b64decode(result["audio_base64"]),
            sample_rate=result.get("sample_rate"),
            provider_metrics=metrics, events=self.events,
        )

    async def cancel(self, *, reason: str = "lost the race") -> CancellationReceipt:
        cancel_sent = now_ms()
        was_running = not self._task.done()
        run_id = await self._discover_run_id()
        if run_id is None:
            # No run id -> nothing was cancelled. NO_EVIDENCE and a leak, never
            # an implicit success (this exact path leaked in the forced audit).
            return CancellationReceipt(
                provider=self.key, job_id=None, was_running=was_running,
                cancel_sent_ms=cancel_sent,
                note="could not discover run id in runs list",
            )
        try:
            data = await asyncio.to_thread(self._backend.rest_app, "DELETE", f"runs/{run_id}")
            cancel_ack = now_ms()
        except Exception as exc:  # noqa: BLE001
            return CancellationReceipt(
                provider=self.key, job_id=run_id, was_running=was_running,
                cancel_sent_ms=cancel_sent, note=f"cancel call failed: {exc}",
            )
        receipt = CancellationReceipt(
            provider=self.key, job_id=run_id, was_running=was_running,
            cancel_sent_ms=cancel_sent, cancel_ack_ms=cancel_ack,
            evidence=CancellationEvidence.PROVIDER_ACK.value,
            cancel_scope="request",
            note=f"cancel ack={str(data.get('raw', 'OK'))[:60]!r}; Cerebrium's "
                 "run status API does not reconcile, so the strongest available "
                 "proof is the in-flight request channel closing",
        )
        # Strongest observable proof: the in-flight sync request breaks when
        # the handler stops. This is REQUEST_CHANNEL_CLOSED evidence — an
        # honest proxy, NOT a confirmed terminal state or billing stop.
        if was_running:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=30.0)
                # The request completed normally after the cancel ack: the run
                # finished anyway; nothing is left running.
                receipt.evidence = CancellationEvidence.REQUEST_CHANNEL_CLOSED.value
                receipt.leaked = False
                receipt.note += "; request completed before the cancel took effect"
            except TimeoutError:
                receipt.note += "; request still open 30s after cancel"
            except Exception:  # noqa: BLE001 - connection broke = handler stopped
                receipt.evidence = CancellationEvidence.REQUEST_CHANNEL_CLOSED.value
                receipt.leaked = False
        else:
            # The sync request already finished; there is nothing left to stop.
            receipt.evidence = CancellationEvidence.REQUEST_CHANNEL_CLOSED.value
            receipt.leaked = False
        if not receipt.leaked:
            receipt.terminal_ms = now_ms()
            billed_ms = receipt.terminal_ms - self.submit_ms
            receipt.execution_ms_before_cancel = billed_ms
            receipt.estimated_cost_usd = self._backend.provider.billed_cost(
                billed_ms / 1000.0
            )
        return receipt


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CerebriumBackend(Backend):
    def __init__(self, provider: Provider, request: dict[str, Any]) -> None:
        super().__init__(provider, request)
        self.project_id = provider.extra["project_id"]
        self.app = provider.extra["app"]
        self.function = provider.extra.get("function", "tts")
        self._token: str | None = None
        self._inference_key: str | None = None
        self._app_id: str | None = provider.extra.get("app_id")
        self.claimed_run_ids: set[str] = set()

    @property
    def headers(self) -> dict[str, str]:
        """Platform-API headers (rest.cerebrium.ai: runs list, cancel, apps)."""

        if self._token is None:
            self._token = load_cerebrium_token()
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    @property
    def inference_headers(self) -> dict[str, str]:
        if self._inference_key is None:
            self._inference_key = load_cerebrium_inference_key(self.project_id)
        return {
            "Authorization": f"Bearer {self._inference_key}",
            "Content-Type": "application/json",
        }

    @property
    def inference_base(self) -> str:
        return f"https://api.cerebrium.ai/v4/{self.project_id}/{self.app}"

    def app_id(self) -> str:
        if self._app_id is None:
            resp = requests.get(
                f"{_REST}/projects/{self.project_id}/apps", headers=self.headers, timeout=30
            )
            resp.raise_for_status()
            apps = resp.json()
            match = [a for a in apps if self.app in (a.get("name"), a.get("id"))]
            if not match:
                raise NotDeployedError(
                    f"app {self.app!r} not found in project {self.project_id} "
                    f"(have: {[a.get('name') for a in apps]}) — run `cerebrium deploy`"
                )
            self._app_id = match[0].get("id") or match[0]["name"]
        return self._app_id

    def rest_app(self, verb: str, path: str, json: dict[str, Any] | None = None,
                 timeout: float = 30.0) -> dict[str, Any] | list:
        url = f"{_REST}/projects/{self.project_id}/apps/{self.app_id()}/{path}"
        resp = requests.request(verb, url, headers=self.headers, json=json, timeout=timeout)
        if resp.status_code in (401, 403):
            # Session expired mid-run: refresh once and retry.
            self._token = refresh_cerebrium_token()
            resp = requests.request(
                verb, url, headers=self.headers, json=json, timeout=timeout
            )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError:
            return {"raw": resp.text}
        if isinstance(body, dict):
            return body.get("item", body)
        return body

    def _body(self) -> dict[str, Any]:
        return {
            "text": self.request["text"],
            "voice_id": self.request["voice_id"],
            "voice_dir": self.request["voice_dir"],
            "language": self.request.get("language", "English"),
        }

    def _sync_post(self) -> dict[str, Any]:
        resp = requests.post(
            f"{self.inference_base}/{self.function}",
            headers=self.inference_headers, json=self._body(),
            timeout=(15, 600),  # generous read timeout; result() enforces the cap
        )
        resp.raise_for_status()
        return resp.json()

    async def submit(self) -> JobHandle:
        if not self.provider.deployed:
            raise NotDeployedError(
                "cerebrium MOSS app not deployed. Stage 1:\n"
                "  cd deploy/cerebrium && cerebrium deploy\n"
                "then set providers.cerebrium.deployed: true in config/benchmark.yaml"
            )
        # Resolve auth before timing starts so key fetches don't count as latency.
        _ = self.inference_headers
        submit_ms = now_ms()
        submitted_at = datetime.now(timezone.utc)
        task = asyncio.create_task(asyncio.to_thread(self._sync_post))
        return CerebriumJob(self, task, submit_ms=submit_ms, submitted_at=submitted_at)
