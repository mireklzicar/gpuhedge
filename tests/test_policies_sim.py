"""Integration tests: the real policy engines over simulated providers.

These run the exact code paths used against real clouds (live_hedge.py,
state_aware.py) with deterministic simulated latencies — no accounts, no
network, sub-second wall time per test."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from gpuhedge import load_config
from gpuhedge.backends.sim_backend import reset_sim_streams
from gpuhedge.benchmark.live_hedge import run_hedged_request
from gpuhedge.benchmark.state_aware import run_state_aware_request
from gpuhedge.telemetry import CostLedger, TraceWriter

SCALE = 0.1  # real seconds per simulated second in these tests


def _sim_config(tmp_path: Path, primary_sim: dict, hedge_sim: dict,
                fallback_sim: dict | None = None):
    providers = {
        "prim": {"role": "primary", "gpu": "SIM", "region": "sim",
                 "gpu_rate_per_s": 0.0003, "billed_rate_per_s": 0.0003,
                 "deployed": True, "adapter": "sim",
                 "sim": {"time_scale": SCALE, **primary_sim}},
        "back": {"role": "hedge", "gpu": "SIM", "region": "sim",
                 "gpu_rate_per_s": 0.0007, "billed_rate_per_s": 0.0007,
                 "deployed": True, "adapter": "sim",
                 "sim": {"time_scale": SCALE, **hedge_sim}},
    }
    if fallback_sim is not None:
        providers["fall"] = {
            "role": "fallback", "gpu": "SIM", "region": "sim",
            "gpu_rate_per_s": 0.0007, "billed_rate_per_s": 0.0007,
            "deployed": True, "adapter": "sim",
            "sim": {"time_scale": SCALE, **fallback_sim}}
    spec = {
        "model": "sim-test",
        "request": {"text": "hi", "voice_id": "v", "voice_dir": "v",
                    "language": "English", "reference": "none"},
        "providers": providers,
        "policy": {"primary": "prim", "hedge_after_ms": 10_000,
                   "hedge_choose_one_of": ["back"], "max_active_jobs": 2,
                   "winner": "first_valid_output"},
        "slo": {"primary_deadline_s": 60, "report_deadlines_s": [30, 60]},
        "timeouts_s": {"moss": 300},
        "stages": {},
        "budget": {"currency": "USD", "operational_stop": 1e6,
                   "absolute_ceiling": 1e6, "gates": {}},
        "qualification": {},
        "output": {"trace_dir": str(tmp_path)},
    }
    path = tmp_path / "sim.yaml"
    path.write_text(yaml.safe_dump(spec))
    return load_config(path)


ALWAYS_SLOW = {"seed": 1, "fast": {"p": 0.0, "queue_s": [0, 0], "run_s": [1, 1]},
               "slow": {"queue_s": [5.0, 5.0], "run_s": [80.0, 80.0]}}
ALWAYS_FAST = {"seed": 2, "fast": {"p": 1.0, "queue_s": [0.5, 0.5],
                                   "run_s": [2.0, 2.0]}}
STEADY = {"seed": 3, "fast": {"p": 1.0, "queue_s": [0.5, 0.5],
                              "run_s": [15.0, 15.0]}}
FAST_INVALID = {"seed": 4, "invalid_p": 1.0,
                "fast": {"p": 1.0, "queue_s": [0.5, 0.5], "run_s": [2.0, 2.0]}}


def _io(tmp_path: Path, cfg):
    return (CostLedger(cfg, ledger_path=tmp_path / "ledger.jsonl"),
            TraceWriter(tmp_path / "trace.jsonl"))


# ------------------------------------------------------------ fixed hedge
def test_fixed_hedge_slow_primary_loses_and_is_cancelled(tmp_path):
    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_SLOW, STEADY)
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_hedged_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        hedge_after_ms=int(10_000 * SCALE), timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    assert rec["hedge_launched"] is True
    assert rec["winner"] == "back"
    c = rec["cancellation"]
    assert c and c["provider"] == "prim" and c["terminal_status"] == "CANCELLED"
    assert not c["leaked"]
    # end-to-end includes the hedge launch offset: ~10 s + ~15.5 s (sim time)
    total_sim_s = rec["winner_total_ms"] / 1000.0 / SCALE
    assert 20.0 < total_sim_s < 35.0


def test_fixed_hedge_fast_primary_never_hedges(tmp_path):
    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_FAST, STEADY)
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_hedged_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        hedge_after_ms=int(10_000 * SCALE), timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    assert rec["hedge_launched"] is False
    assert rec["winner"] == "prim"
    assert rec["cancellation"] is None


def test_fixed_hedge_rejects_fast_invalid_result(tmp_path):
    reset_sim_streams()
    cfg = _sim_config(tmp_path, FAST_INVALID, STEADY)
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_hedged_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        hedge_after_ms=int(10_000 * SCALE), timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    # primary returned quickly but malformed -> hedge must win
    assert rec["winner"] == "back"
    assert rec["hedge_launched"] is True


# ------------------------------------------------------------ state aware
def test_cutover_fires_while_primary_queued(tmp_path):
    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_SLOW, STEADY)  # 5 s queue > 2.5 s poll
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_state_aware_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        queue_cutover_ms=int(2_500 * SCALE), safety_hedge_ms=int(8_500 * SCALE),
        timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    assert rec["state_at_poll"] == "IN_QUEUE"
    assert rec["cutover_fired"] is True
    assert rec["safety_hedge_fired"] is False
    assert rec["winner"] == "back"
    c = rec["cancellation"]
    assert c and c["provider"] == "prim" and not c["leaked"]
    # cancelled while queued: no execution before cancel, no estimated cost
    assert not c["was_running"]
    assert (c["estimated_cost_usd"] or 0.0) == 0.0
    total_sim_s = rec["winner_total_ms"] / 1000.0 / SCALE
    assert total_sim_s < 25.0  # ~2.5 + ~15.5


def test_cutover_keeps_running_primary(tmp_path):
    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_FAST, STEADY)  # 0.5 s queue, 2 s run
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_state_aware_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        queue_cutover_ms=int(1_000 * SCALE), safety_hedge_ms=int(8_500 * SCALE),
        timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    assert rec["cutover_fired"] is False
    assert rec["winner"] == "prim"
    assert rec["cancellation"] is None


def test_safety_hedge_rescues_slow_running_primary(tmp_path):
    reset_sim_streams()
    slow_running = {"seed": 5, "fast": {"p": 1.0, "queue_s": [0.5, 0.5],
                                        "run_s": [90.0, 90.0]}}
    cfg = _sim_config(tmp_path, slow_running, STEADY)
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_state_aware_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        queue_cutover_ms=int(2_500 * SCALE), safety_hedge_ms=int(8_500 * SCALE),
        timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    assert rec["state_at_poll"] == "IN_PROGRESS"  # running, just slow
    assert rec["cutover_fired"] is False
    assert rec["safety_hedge_fired"] is True
    assert rec["winner"] == "back"
    c = rec["cancellation"]
    assert c and c["provider"] == "prim" and c["was_running"]


# --------------------------------------------------------------- cascade
SLOW_HEDGE = {"seed": 6, "fast": {"p": 0.0, "queue_s": [0, 0], "run_s": [1, 1]},
              "slow": {"queue_s": [3.0, 3.0], "run_s": [90.0, 90.0]}}
RELIABLE = {"seed": 7, "fast": {"p": 1.0, "queue_s": [1.0, 1.0],
                                "run_s": [14.0, 14.0]}}


def _cascade(cfg, tmp_path, **kw):
    from gpuhedge.benchmark.cascade import run_cascade_request

    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_cascade_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        fallback_key="fall",
        queue_cutover_ms=int(2_500 * SCALE), safety_hedge_ms=int(8_500 * SCALE),
        escalate_after_ms=int(25_000 * SCALE), timeout_s=300 * SCALE, **kw))
    trace.close()
    ledger.close()
    return rec


def test_cascade_escalates_past_hedge_tail(tmp_path):
    # primary queued at the poll -> cutover to the hedge; the hedge then has
    # its own ~93 s tail -> the fallback launches at 25 s and wins.
    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_SLOW, SLOW_HEDGE, RELIABLE)
    rec = _cascade(cfg, tmp_path)

    assert rec["cutover_fired"] is True
    assert rec["escalation_fired"] is True
    assert rec["winner"] == "fall"
    total_sim_s = rec["winner_total_ms"] / 1000.0 / SCALE
    assert 38.0 < total_sim_s < 44.0  # ~25 + 1 + 14
    # both losers produced receipts: the queued primary and the running hedge
    receipts = rec["cancellations"]
    assert {r["provider"] for r in receipts} == {"prim", "back"}
    prim = next(r for r in receipts if r["provider"] == "prim")
    assert prim["cancel_scope"] == "queued_job"
    assert prim["confirmed_terminal"] and not prim["leaked"]
    assert prim["evidence"] == "confirmed_terminal"
    assert (prim["estimated_cost_usd"] or 0.0) == 0.0
    # at most two jobs were ever live: the fallback was only submitted after
    # the primary's cancellation was confirmed
    assert len(rec["submits"]) == 3


def test_cascade_fast_primary_never_fires(tmp_path):
    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_FAST, SLOW_HEDGE, RELIABLE)
    rec = _cascade(cfg, tmp_path)

    assert rec["winner"] == "prim"
    assert not rec["cutover_fired"]
    assert not rec["safety_hedge_fired"]
    assert not rec["escalation_fired"]
    assert rec["cancellation"] is None


def test_cascade_two_job_cap_blocks_escalation(tmp_path):
    # primary runs (kept) but is slow -> safety hedge fires; both stay live
    # through the 25 s escalation point, so the fallback must NOT launch.
    reset_sim_streams()
    slow_running = {"seed": 8, "fast": {"p": 1.0, "queue_s": [0.5, 0.5],
                                        "run_s": [40.0, 40.0]}}
    hedge_30s = {"seed": 9, "fast": {"p": 1.0, "queue_s": [0.5, 0.5],
                                     "run_s": [28.0, 28.0]}}
    cfg = _sim_config(tmp_path, slow_running, hedge_30s, RELIABLE)
    rec = _cascade(cfg, tmp_path)

    assert rec["safety_hedge_fired"] is True
    assert rec["escalation_fired"] is False
    assert rec["escalation_skipped_at_capacity"] is True
    assert rec["winner"] == "back"          # hedge lands at ~8.5+28.5 s
    assert len(rec["submits"]) == 2


def test_cutover_keeps_primary_in_race_when_cancel_fails(tmp_path, monkeypatch):
    # The feedback edge case: remote cancel fails AND the hedge returns
    # invalid output. The primary's result task must stay in the race and win
    # rather than the router returning nothing.
    from gpuhedge.backends.sim_backend import SimJob

    async def broken_cancel(self, *, reason="lost the race"):
        raise RuntimeError("cancel endpoint down")

    monkeypatch.setattr(SimJob, "cancel", broken_cancel)
    reset_sim_streams()
    queued_but_finishes = {"seed": 10,
                           "fast": {"p": 0.0, "queue_s": [0, 0], "run_s": [1, 1]},
                           "slow": {"queue_s": [5.0, 5.0], "run_s": [10.0, 10.0]}}
    cfg = _sim_config(tmp_path, queued_but_finishes, FAST_INVALID)
    ledger, trace = _io(tmp_path, cfg)
    rec = asyncio.run(run_state_aware_request(
        cfg, ledger, trace, primary_key="prim", hedge_key="back",
        queue_cutover_ms=int(2_500 * SCALE), safety_hedge_ms=int(8_500 * SCALE),
        timeout_s=300 * SCALE))
    trace.close()
    ledger.close()

    assert rec["cutover_fired"] is True
    assert rec["winner"] == "prim"          # rescued by the leaked primary
    c = rec["cancellation"]
    assert c["leaked"] is True              # the failed cancel is an honest leak
    assert c["evidence"] == "no_evidence"


# ------------------------------------------------------------------ router
def test_router_over_sim(tmp_path):
    from gpuhedge.policies import StateAwarePolicy
    from gpuhedge.router import Router

    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_SLOW, STEADY)
    with Router(primary="prim", hedge="back",
                policy=StateAwarePolicy(queue_cutover_ms=int(2_500 * SCALE),
                                        safety_hedge_ms=int(8_500 * SCALE)),
                config=cfg, trace_path=tmp_path / "router.jsonl") as router:
        outcome = asyncio.run(router.run())
    assert outcome.ok and outcome.winner == "back" and outcome.hedged
    assert outcome.cancellation and outcome.cancellation["provider"] == "prim"


def test_router_cascade_policy_and_provider_check(tmp_path):
    from gpuhedge.policies import CascadePolicy
    from gpuhedge.router import Router

    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_SLOW, SLOW_HEDGE, RELIABLE)
    policy = CascadePolicy(queue_cutover_ms=int(2_500 * SCALE),
                           safety_hedge_ms=int(8_500 * SCALE),
                           escalate_after_ms=int(25_000 * SCALE))
    # a cascade needs three providers
    with pytest.raises(ValueError):
        Router(primary="prim", hedge="back", policy=policy, config=cfg)
    with Router(primary="prim", hedge="back", fallback="fall", policy=policy,
                config=cfg, trace_path=tmp_path / "router.jsonl") as router:
        outcome = asyncio.run(router.run())
    assert outcome.ok and outcome.winner == "fall" and outcome.hedged
    assert outcome.record["escalation_fired"] is True


def test_custom_policy_protocol(tmp_path):
    """A third-party policy is dispatched through execute(), no isinstance."""

    from gpuhedge.router import Router

    class EchoPolicy:
        min_providers = 1

        async def execute(self, ctx):
            return {"winner": ctx.primary, "winner_total_ms": 1.0,
                    "policy": "custom:echo"}

    reset_sim_streams()
    cfg = _sim_config(tmp_path, ALWAYS_FAST, STEADY)
    with Router(primary="prim", policy=EchoPolicy(), config=cfg,
                trace_path=tmp_path / "router.jsonl") as router:
        outcome = asyncio.run(router.run())
    assert outcome.ok and outcome.winner == "prim"
    assert outcome.record["policy"] == "custom:echo"


# ------------------------------------------------------- replay: cutover
def test_queue_cutover_replay_math():
    from gpuhedge.benchmark.replay import (
        Round,
        evaluate_queue_cutover,
        wilson_interval,
    )

    cfg = load_config()  # real packaged config: runpod/cerebrium rates
    rounds = [
        # FlashBoot hit: queue 1.5 s, done at 6 s -> stays on primary
        Round(1, 1, {"runpod": 6.0, "cerebrium": 20.0},
              {"runpod": 6.0, "cerebrium": 20.0}, {"runpod": 1.5}),
        # miss: queue 12 s -> cutover at 2.5 s, hedge lands at 22.5 s
        Round(2, 1, {"runpod": 110.0, "cerebrium": 20.0},
              {"runpod": 110.0, "cerebrium": 20.0}, {"runpod": 12.0}),
    ]
    res = evaluate_queue_cutover(cfg, rounds, "runpod", "cerebrium", [30, 60])
    assert res.n == 2 and res.hedge_rate == pytest.approx(0.5)
    assert res.max_s == pytest.approx(22.5)
    assert res.miss_counts[60] == 0
    # cost: round 1 = 6 s runpod; round 2 = 20 s cerebrium only (queued cancel free)
    expected = (6.0 * 0.000306 + 20.0 * 0.000736) / 2
    assert res.mean_cost_usd == pytest.approx(expected, rel=1e-6)
    # billed adds runpod idle only where its worker started (round 1)
    expected_billed = expected + (60.0 * 0.000306) / 2
    assert res.mean_billed_usd == pytest.approx(expected_billed, rel=1e-6)

    lo, hi = wilson_interval(0, 36)
    assert lo == 0.0 and 0.08 < hi < 0.11


def test_cascade_replay_math():
    from gpuhedge.benchmark.replay import Round, evaluate_cascade

    cfg = load_config()  # real packaged config: runpod/cerebrium/modal rates
    c_p = cfg.provider("runpod").billed_rate_per_s
    c_h = cfg.provider("cerebrium").billed_rate_per_s
    c_f = cfg.provider("modal").billed_rate_per_s
    rounds = [
        # FlashBoot hit -> stays on primary
        Round(1, 1, {"runpod": 6.0, "cerebrium": 20.0, "modal": 30.0},
              {"runpod": 6.0, "cerebrium": 20.0, "modal": 30.0},
              {"runpod": 1.5}),
        # miss -> cutover at 2.5 s; hedge lands at 22.5 s, before escalation
        Round(2, 1, {"runpod": 110.0, "cerebrium": 20.0, "modal": 30.0},
              {"runpod": 110.0, "cerebrium": 20.0, "modal": 30.0},
              {"runpod": 12.0}),
        # miss AND a hedge tail (the observed 104 s case) -> the fallback
        # launches at 25 s and wins at 55 s: the >60 s miss disappears
        Round(3, 1, {"runpod": 110.0, "cerebrium": 104.0, "modal": 30.0},
              {"runpod": 110.0, "cerebrium": 104.0, "modal": 30.0},
              {"runpod": 12.0}),
    ]
    res = evaluate_cascade(cfg, rounds, "runpod", "cerebrium", "modal",
                           [30, 60], escalate_s=25.0)
    assert res.n == 3 and res.hedge_rate == pytest.approx(2 / 3)
    assert res.max_s == pytest.approx(55.0)
    assert res.miss_counts[60] == 0        # the 104 s tail no longer misses
    expected = (c_p * 6.0                          # round 1
                + c_h * 20.0                       # round 2 (queued cancel free)
                + c_h * 52.5 + c_f * 30.0) / 3     # round 3: hedge to winner + fallback
    assert res.mean_cost_usd == pytest.approx(expected, rel=1e-6)


# ------------------------------------------------------------ http adapter
def test_http_adapter_units(monkeypatch):
    from gpuhedge.backends import JobState
    from gpuhedge.backends.http_backend import HttpBackend, _substitute_env, dig
    from gpuhedge.config import Provider

    assert dig({"a": {"b": [{"c": 7}]}}, "a.b.0.c") == 7
    assert dig({"a": 1}, "$.a") == 1
    assert dig({"a": 1}, "missing") is None

    monkeypatch.setenv("T_TOKEN", "sekrit")
    assert _substitute_env("Bearer ${T_TOKEN}") == "Bearer sekrit"

    provider = Provider(
        key="svc", role="hedge", gpu="L40S", billed_rate_per_s=0.0007,
        gpu_rate_per_s=0.0005, region="x", deployed=True,
        extra={"adapter": "http", "http": {
            "submit": {"method": "POST", "url": "https://x/jobs",
                       "job_id_path": "id"},
            "status": {"url": "https://x/jobs/{job_id}", "state_path": "status",
                       "state_map": {"working": "IN_PROGRESS",
                                     "done": "COMPLETED"}},
        }},
    )
    be = HttpBackend(provider, {"text": "hi"})
    assert be.map_state("working") is JobState.IN_PROGRESS
    assert be.map_state("done") is JobState.COMPLETED
    assert be.map_state("QUEUED") is JobState.QUEUED      # default map
    assert be.map_state("wat") is JobState.UNKNOWN
