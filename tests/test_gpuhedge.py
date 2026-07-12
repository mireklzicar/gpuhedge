from __future__ import annotations

import asyncio
import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from gpuhedge import __version__, load_config
from gpuhedge.backends import (
    Backend,
    JobHandle,
    JobState,
    NotDeployedError,
    ProviderResult,
    build_backend,
)
from gpuhedge.backends.base import now_ms
from gpuhedge.backends.cerebrium_backend import CerebriumBackend
from gpuhedge.benchmark.round import run_paired_round
from gpuhedge.telemetry import BudgetExceeded, CostLedger, TraceWriter
from gpuhedge.validators import validate_wav


def _wav_bytes(seconds: float = 5.0, sr: int = 24000, amp: float = 0.2) -> bytes:
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    tone = (amp * np.sin(2 * np.pi * 220 * t)).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, tone, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# --------------------------------------------------------------------- config
def test_version_and_config():
    assert __version__ == "0.1.0"
    cfg = load_config()
    assert set(cfg.providers) == {"runpod", "modal", "cerebrium"}
    assert cfg.budget.absolute_ceiling == 50.0
    assert cfg.budget.operational_stop == 45.0
    assert cfg.budget.gates["moss_trace_complete"] == 29.0
    assert cfg.moss_timeout_s() == 300.0
    assert isinstance(cfg.provider("cerebrium").deployed, bool)


# --------------------------------------------------------------------- ledger
def test_ledger_gate_and_stop(tmp_path: Path):
    cfg = load_config()
    ledger = CostLedger(cfg, ledger_path=tmp_path / "ledger.jsonl")
    runpod = cfg.provider("runpod")
    # A single 300 s cold+idle round is cheap; the $4 qualification gate holds.
    ledger.charge(runpod, billed_seconds=300, stage="qualify")
    ledger.check_gate("qualification_complete")
    # Piling on charges must eventually trip the operational stop.
    with pytest.raises(BudgetExceeded):
        for _ in range(100000):
            ledger.charge(cfg.provider("modal"), billed_seconds=300, stage="moss")
    ledger.close()


def test_ledger_resumes(tmp_path: Path):
    cfg = load_config()
    path = tmp_path / "ledger.jsonl"
    a = CostLedger(cfg, ledger_path=path)
    a.charge(cfg.provider("runpod"), billed_seconds=100, stage="moss")
    total = a.projected_total
    a.close()
    b = CostLedger(cfg, ledger_path=path)  # reload
    assert b.projected_total == pytest.approx(total)
    b.close()


# --------------------------------------------------------------- cost monitor
def test_cost_monitor_deltas(tmp_path: Path):
    from gpuhedge.telemetry.costs import CostMonitor, CostReading

    cfg = load_config()
    balances = {"runpod": 20.0, "modal": 0.0}

    def runpod():
        return CostReading("runpod", "balance", balances["runpod"])

    def modal():
        return CostReading("modal", "app_cost", balances["modal"])

    def cerebrium():
        return CostReading("cerebrium", "unavailable", None, "no API")

    readers = {"runpod": runpod, "modal": modal, "cerebrium": cerebrium}
    mon = CostMonitor(cfg, path=tmp_path / "costs.jsonl", readers=readers)
    first = mon.snapshot("start")
    assert first["is_baseline"] and first["actual_spend_since_baseline"] == {
        "runpod": 0.0, "modal": 0.0,
    }

    balances["runpod"] = 18.5   # balance falls -> spent 1.50
    balances["modal"] = 0.75    # app cost rises -> spent 0.75
    snap = mon.snapshot("block-1")
    assert snap["actual_spend_since_baseline"] == {"runpod": 1.5, "modal": 0.75}
    assert "cerebrium" not in snap["actual_spend_since_baseline"]
    mon.close()

    # Baseline survives a restart (resumable like the ledger).
    mon2 = CostMonitor(cfg, path=tmp_path / "costs.jsonl", readers=readers)
    assert mon2.baseline == {"runpod": 20.0, "modal": 0.0}
    snap2 = mon2.snapshot("resumed")
    assert snap2["actual_spend_since_baseline"]["runpod"] == 1.5
    mon2.close()


# ------------------------------------------------------------------ validator
def test_validate_wav():
    assert validate_wav(_wav_bytes()).valid
    assert not validate_wav(None).valid
    assert not validate_wav(b"not a wav").valid
    silent = _wav_bytes(amp=0.0)
    v = validate_wav(silent)
    assert not v.valid and any("silent" in r for r in v.reasons)


# -------------------------------------------------------------------- factory
def test_factory_and_not_deployed():
    import dataclasses

    cfg = load_config()
    undeployed = dataclasses.replace(cfg.provider("cerebrium"), deployed=False)
    be = build_backend(undeployed, cfg.request)
    assert isinstance(be, CerebriumBackend)
    assert be.available() is False
    with pytest.raises(NotDeployedError):
        asyncio.run(be.submit())


# ----------------------------------------------------- paired round (offline)
class _FakeJob(JobHandle):
    def __init__(self, key, wall_s, audio, submit_ms):
        super().__init__(key, submit_ms=submit_ms)
        self._wall_s = wall_s
        self._audio = audio

    async def result(self, timeout_s: float) -> ProviderResult:
        self._record("result", f"{self._wall_s}s")
        state = JobState.COMPLETED if self._audio else JobState.TIMEOUT
        return ProviderResult(self.key, state, self._wall_s, audio=self._audio,
                              provider_metrics={"already_loaded": False}, events=self.events)


class _FakeBackend(Backend):
    def __init__(self, provider, request, wall_s, audio):
        super().__init__(provider, request)
        self._wall_s = wall_s
        self._audio = audio

    def available(self) -> bool:
        return True

    async def submit(self) -> JobHandle:
        return _FakeJob(self.key, self._wall_s, self._audio, submit_ms=now_ms())


def test_paired_round(tmp_path: Path):
    cfg = load_config()
    wav = _wav_bytes()
    backends = [
        _FakeBackend(cfg.provider("runpod"), cfg.request, 7.0, wav),
        _FakeBackend(cfg.provider("modal"), cfg.request, 30.0, wav),
        _FakeBackend(cfg.provider("cerebrium"), cfg.request, 300.0, None),  # censored
    ]
    ledger = CostLedger(cfg, ledger_path=tmp_path / "ledger.jsonl")
    trace = TraceWriter(tmp_path / "rounds.jsonl")
    result = asyncio.run(run_paired_round(
        cfg, backends, ledger, trace, round_id=1, block=1, warm_companion=False,
    ))
    trace.close()
    ledger.close()

    assert result.winner == "runpod"          # fastest valid
    assert result.winner_wall_s == 7.0
    assert result.outcomes["cerebrium"].state == "TIMEOUT"
    assert not result.outcomes["cerebrium"].valid
    assert (tmp_path / "rounds.jsonl").read_text().strip()  # a trace line was written
