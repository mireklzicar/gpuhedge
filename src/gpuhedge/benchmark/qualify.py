"""Stage 1 — qualify Cerebrium before committing the benchmark (max $4).

Every criterion in ``config.qualification`` must pass or the third-provider slot
switches to Beam rather than burning the budget on undocumented behaviour
(benchmarks/2026-07-moss/methodology.md, Stage 1). Cancellation is exercised at three stages
(queued / loading / generating) and an HTTP 200 is NOT accepted as success — we
poll to a confirmed terminal state and estimate whether billing stopped.

This is also where the Cerebrium adapter's VERIFY-IN-STAGE-1 async/cancel REST
paths are confirmed against the live deployment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from gpuhedge.backends import build_backend
from gpuhedge.backends.cerebrium_backend import CerebriumBackend
from gpuhedge.config import BenchmarkConfig
from gpuhedge.telemetry import CostLedger, TraceWriter
from gpuhedge.validators import validate_wav

Logger = Callable[[str], None]


@dataclass
class Criterion:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class QualificationReport:
    deployed: bool
    qualified: bool = False
    criteria: list[Criterion] = field(default_factory=list)
    projected_spend_usd: float = 0.0
    guidance: str = ""

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.criteria.append(Criterion(name, passed, detail))


async def run_qualification(
    config: BenchmarkConfig,
    *,
    log: Logger = print,
    dry_run: bool = False,
    sleep=asyncio.sleep,
) -> QualificationReport:
    q = config.qualification
    provider = config.provider("cerebrium")
    backend = build_backend(provider, config.request)
    assert isinstance(backend, CerebriumBackend)

    if not provider.deployed:
        return QualificationReport(
            deployed=False,
            guidance=(
                "Cerebrium MOSS app not deployed. Stage 1:\n"
                "  1) cd deploy/cerebrium && cerebrium deploy\n"
                "  2) cerebrium run main.py::seed   (pre-load weights to storage)\n"
                "  3) set providers.cerebrium.deployed: true in config/benchmark.yaml\n"
                "  4) re-run: gpuhedge qualify --go"
            ),
        )

    log(f"Stage 1 — Cerebrium qualification (max ${q['max_spend']:.0f})")
    if dry_run:
        log("  dry-run: not submitting any jobs.")
        return QualificationReport(deployed=True, guidance="dry-run")

    ledger = CostLedger(config)
    trace = TraceWriter(config.trace_dir() / "qualification.jsonl")
    report = QualificationReport(deployed=True)

    try:
        await _test_provisioning(backend, config, q, ledger, trace, report, log, sleep)
        await _test_output_and_load(backend, config, q, ledger, trace, report, log)
        await _test_cancellation(backend, config, q, ledger, trace, report, log, sleep)
    except Exception as exc:  # noqa: BLE001
        report.add("harness", False, f"exception: {exc}")
    finally:
        report.projected_spend_usd = ledger.projected_total
        report.qualified = bool(report.criteria) and all(c.passed for c in report.criteria)
        trace.write({"kind": "qualification_report", **asdict(report)})
        trace.close()
        ledger.close()
    log(f"  -> {'QUALIFIED' if report.qualified else 'NOT QUALIFIED (switch to Beam)'}; "
        f"projected ${report.projected_spend_usd:.2f}")
    return report


async def _test_provisioning(backend, config, q, ledger, trace, report, log, sleep) -> None:
    attempts = int(q["provisioning_attempts"])
    need = int(q["provisioning_success_min"])
    deadline = float(q["provisioning_deadline_s"])
    log(f"  [provisioning] {attempts} cold L40S requests, need {need} within {deadline:.0f}s")
    got = 0
    for i in range(attempts):
        try:
            handle = await backend.submit()
            result = await handle.result(deadline)
            ok = result.ok
        except Exception as exc:  # noqa: BLE001
            ok = False
            log(f"    attempt {i + 1}: error {str(exc)[:120]}")
            result = None
        if result is not None:
            ledger.charge(backend.provider, getattr(result, "wall_s", 0.0),
                          stage="qualify", note="provisioning")
            got += int(ok)
            log(f"    attempt {i + 1}: {'container+result' if ok else result.state.value}")
        if i < attempts - 1:
            await sleep(config.stages['moss'].get('cooldown_s', 45))
    report.add("provisioning", got >= need, f"{got}/{attempts} within {deadline:.0f}s")


async def _test_output_and_load(backend, config, q, ledger, trace, report, log) -> None:
    log("  [output/load] one cold request -> WAV + weight-load provenance")
    handle = await backend.submit()
    result = await handle.result(config.moss_timeout_s())
    ledger.charge(backend.provider, result.wall_s, stage="qualify", note="output test")
    v = validate_wav(result.audio)
    report.add("output_wav", v.valid, "; ".join(v.reasons) or f"{v.duration_s}s ok")
    metrics = result.provider_metrics or {}
    downloaded = bool(metrics.get("downloaded_weights", False))
    if q.get("require_no_weight_download", True):
        report.add("no_weight_download", not downloaded,
                   f"load_seconds={metrics.get('load_seconds')}, downloaded={downloaded}")
    trace.write({"kind": "qualify_output", "metrics": metrics,
                 "validation": asdict(v), "wall_s": result.wall_s})


async def _test_cancellation(backend, config, q, ledger, trace, report, log, sleep) -> None:
    """Cancel at queued / loading / generating and require a terminal receipt."""

    stages = q.get("cancel_stages", ["queued", "loading", "generating"])
    # Approximate the stage by how long we let the job run before cancelling.
    delays = {"queued": 0.0, "loading": 3.0, "generating": 25.0}
    all_terminal = True
    billing_ok = True
    for stage_name in stages:
        handle = await backend.submit()
        await sleep(delays.get(stage_name, 0.0))
        receipt = await handle.cancel(reason=f"qualify:{stage_name}")
        terminal = receipt.terminal_status is not None and not receipt.leaked
        all_terminal = all_terminal and terminal
        # Billing "stopped" heuristic: terminal reached and execution bounded.
        billing_ok = billing_ok and (terminal and not receipt.leaked)
        if receipt.estimated_cost_usd:
            ledger.charge_usd(backend.provider.key, receipt.estimated_cost_usd,
                              stage="qualify", note=f"cancel {stage_name}")
        log(f"    cancel@{stage_name}: terminal={receipt.terminal_status} "
            f"leaked={receipt.leaked} ack={receipt.ack_latency_ms()}ms "
            f"->terminal={receipt.cancel_to_terminal_ms()}ms")
        trace.write({"kind": "qualify_cancel", "stage": stage_name, "receipt": asdict(receipt)})
    if q.get("require_cancel_terminal", True):
        report.add("cancel_terminal", all_terminal, f"stages={stages}")
    if q.get("require_billing_stops_on_cancel", True):
        report.add("billing_stops_on_cancel", billing_ok, "terminal within poll window")
