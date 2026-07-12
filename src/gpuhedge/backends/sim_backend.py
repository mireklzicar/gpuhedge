"""No-cloud simulated provider — try the policies without accounts or spend.

``gpuhedge demo`` races simulated providers through the REAL policy engines
(``live_hedge``, ``state_aware``): a bimodal primary with a visible queue
state, a steady hedge, occasional malformed results, and cancellable jobs.
The same backend powers the integration tests, so the demo and the test suite
exercise exactly the code paths that run against real clouds.

Configuration lives in the provider's ``extra`` under ``sim``:

    adapter: sim
    sim:
      seed: 7                 # deterministic per-provider stream
      time_scale: 0.05        # real seconds per simulated second (20x speed)
      fast: {p: 0.67, queue_s: [1.1, 2.0], run_s: [4.0, 6.5]}
      slow: {queue_s: [9.0, 27.0], run_s: [80.0, 95.0]}
      invalid_p: 0.05         # chance a completed result is malformed audio

All times the backend *reports* (``wall_s``, receipts, metrics) are in real
(compressed) seconds; multiply by ``1/time_scale`` to present simulated
seconds. The demo does this at display time.
"""

from __future__ import annotations

import asyncio
import io
import math
import random
from typing import Any

import numpy as np

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

# Policy engines rebuild backends per request, so the per-provider draw
# sequence must survive instance churn: request n on provider p is always the
# same draw for a given config seed, regardless of how backends are pooled.
_STREAM_COUNTERS: dict[str, int] = {}


def reset_sim_streams() -> None:
    """Restart every simulated provider's draw sequence (tests, demo runs)."""

    _STREAM_COUNTERS.clear()


def make_wav(duration_s: float = 1.0, sample_rate: int = 16000,
             *, malformed: bool = False) -> bytes:
    """A small valid (or deliberately near-silent) WAV for the validator."""

    import soundfile as sf

    t = np.linspace(0.0, duration_s, int(sample_rate * duration_s), endpoint=False)
    amplitude = 1e-6 if malformed else 0.3
    audio = (amplitude * np.sin(2 * math.pi * 220.0 * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    return buf.getvalue()


class SimJob(JobHandle):
    def __init__(self, backend: SimBackend, *, submit_ms: float,
                 queue_s: float, run_s: float, valid: bool, mode: str) -> None:
        super().__init__(backend.key, submit_ms=submit_ms)
        self._backend = backend
        scale = backend.time_scale
        self._queue_end_ms = submit_ms + queue_s * scale * 1000.0
        self._done_ms = self._queue_end_ms + run_s * scale * 1000.0
        self._valid = valid
        self._mode = mode
        self._cancelled = asyncio.Event()
        self._id = f"sim-{backend.key}-{backend.counter}"

    def job_id(self) -> str | None:
        return self._id

    async def status(self) -> JobState:
        if self._cancelled.is_set():
            return JobState.CANCELLED
        now = now_ms()
        if now < self._queue_end_ms:
            return JobState.QUEUED
        if now < self._done_ms:
            return JobState.IN_PROGRESS
        return JobState.COMPLETED

    async def result(self, timeout_s: float) -> ProviderResult:
        deadline = self.submit_ms + timeout_s * 1000.0
        wait_until = min(self._done_ms, deadline)
        remaining = max(0.0, wait_until - now_ms()) / 1000.0
        cancel_wait = asyncio.create_task(self._cancelled.wait())
        try:
            await asyncio.wait_for(asyncio.shield(cancel_wait), timeout=remaining)
        except asyncio.TimeoutError:
            pass
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
        wall_s = (now_ms() - self.submit_ms) / 1000.0
        if self._cancelled.is_set():
            self._record("cancelled", "")
            return ProviderResult(provider=self.key, state=JobState.CANCELLED,
                                  wall_s=wall_s, events=self.events)
        if now_ms() < self._done_ms:  # hit the caller's cap
            self._record("timeout", f"{wall_s:.2f}s")
            return ProviderResult(provider=self.key, state=JobState.TIMEOUT,
                                  wall_s=wall_s, events=self.events,
                                  error="simulated cap")
        self._record("result", f"{wall_s:.2f}s")
        sim_wall = wall_s / self._backend.time_scale
        return ProviderResult(
            provider=self.key, state=JobState.COMPLETED, wall_s=wall_s,
            audio=make_wav(malformed=not self._valid),
            sample_rate=16000,
            provider_metrics={
                "sim_mode": self._mode,
                "sim_wall_s": round(sim_wall, 2),
                f"{self.key}_delay_ms": round(
                    (self._queue_end_ms - self.submit_ms)
                    / self._backend.time_scale, 1),
            },
            events=self.events,
        )

    async def cancel(self, *, reason: str = "lost the race") -> CancellationReceipt:
        cancel_sent = now_ms()
        state = await self.status()
        self._cancelled.set()
        ack = now_ms()
        elapsed_sim_s = min(cancel_sent, self._done_ms) - self.submit_ms
        elapsed_sim_s = max(0.0, elapsed_sim_s) / 1000.0 / self._backend.time_scale
        started = cancel_sent >= self._queue_end_ms
        return CancellationReceipt(
            provider=self.key, job_id=self._id,
            was_running=state is JobState.IN_PROGRESS,
            cancel_sent_ms=cancel_sent, cancel_ack_ms=ack, terminal_ms=ack,
            terminal_status="CANCELLED",
            evidence=CancellationEvidence.CONFIRMED_TERMINAL.value,
            cancel_scope="request" if started else "queued_job",
            confirmed_terminal=True,
            billing_stop_confirmed=True,
            leaked=False,
            execution_ms_before_cancel=(
                round((cancel_sent - self._queue_end_ms)
                      / self._backend.time_scale, 1) if started else 0.0
            ),
            estimated_cost_usd=(
                self._backend.provider.billed_cost(elapsed_sim_s,
                                                   include_idle=False)
                if started else 0.0
            ),
            note=f"simulated cancel while {state.value}",
        )


class SimBackend(Backend):
    def __init__(self, provider: Provider, request: dict[str, Any]) -> None:
        super().__init__(provider, request)
        spec = dict(provider.extra.get("sim", {}))
        self.time_scale = float(spec.get("time_scale", 0.05))
        self._fast = spec.get("fast", {"p": 0.7, "queue_s": [1.0, 2.0],
                                       "run_s": [4.0, 7.0]})
        self._slow = spec.get("slow", {"queue_s": [9.0, 27.0],
                                       "run_s": [80.0, 100.0]})
        self._invalid_p = float(spec.get("invalid_p", 0.0))
        self._seed = int(spec.get("seed", 0))
        self.counter = 0

    def _draw(self, n: int) -> tuple[float, float, bool, str]:
        rng = random.Random(self._seed * 1_000_003 + n)
        fast = rng.random() < float(self._fast.get("p", 1.0))
        mode = self._fast if fast else self._slow
        queue_s = rng.uniform(*mode["queue_s"])
        run_s = rng.uniform(*mode["run_s"])
        valid = rng.random() >= self._invalid_p
        return queue_s, run_s, valid, ("fast" if fast else "slow")

    async def submit(self) -> JobHandle:
        n = _STREAM_COUNTERS.get(self.key, 0) + 1
        _STREAM_COUNTERS[self.key] = n
        self.counter = n
        queue_s, run_s, valid, mode = self._draw(n)
        return SimJob(self, submit_ms=now_ms(), queue_s=queue_s, run_s=run_s,
                      valid=valid, mode=mode)

    async def scale_to_zero(self) -> None:
        return None
