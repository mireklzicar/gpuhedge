# Adding a provider

Two paths: describe it in YAML (no code), or write an adapter class.

## Path 1: the generic HTTP adapter (no Python)

If the service exposes submit/status/result/cancel over HTTP, declare it:

```yaml
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
      headers:
        Authorization: "Bearer ${MY_SERVICE_TOKEN}"   # env-var substitution
      submit:
        method: POST
        url: https://example.com/jobs
        body: {input: "{request}"}     # "{request}" -> the request dict
        job_id_path: id                # dotted path into the response JSON
      status:
        url: https://example.com/jobs/{job_id}
        state_path: status
        state_map: {queued: QUEUED, running: IN_PROGRESS,
                    succeeded: COMPLETED, failed: FAILED,
                    cancelled: CANCELLED}
      result:
        url: https://example.com/jobs/{job_id}
        audio_b64_path: output.audio_base64
        metrics_path: output.metrics
        poll_interval_s: 1.0
      cancel:
        method: DELETE
        url: https://example.com/jobs/{job_id}
```

The adapter polls status to terminal, fetches the result, and confirms
cancels by polling — never by trusting the cancel call's status code. If the
service exposes a real queued state in `state_map`, the provider is
queue-cutover capable for free.

## Path 2: a native adapter class

Implement the four-method contract from `backends/base.py`:

```python
class MyBackend(Backend):
    async def submit(self) -> JobHandle: ...

class MyJob(JobHandle):
    async def status(self) -> JobState: ...
    async def result(self, timeout_s: float) -> ProviderResult: ...
    async def cancel(self, *, reason: str = "...") -> CancellationReceipt: ...
```

Register it in `backends/__init__.py:_REGISTRY` and add an optional
dependency extra in `pyproject.toml` — the package must import without your
SDK installed (import it lazily inside the class).

### Honesty requirements (these are reviewed)

1. **Never fabricate lifecycle states.** If the API can't distinguish queued
   from running, return the coarser state and document it in
   `docs/provider-capabilities.md`.
2. **`result()` right-censors**: on cap, return a `TIMEOUT` result, don't
   raise.
3. **Cancel means confirmed terminal.** Poll to a terminal state; set
   `leaked: true` if the window expires. Fill in every receipt field you can
   observe, including provider-reported billed time when the API exposes it.
4. **Record billing surfaces.** If the provider exposes balance/usage APIs,
   add a reader to `telemetry/costs.py` so block reconciliation works.

### Tests

Add deterministic tests using either fake handles (see
`tests/test_gpuhedge.py`) or the simulator pattern
(`tests/test_policies_sim.py`). Minimum: a submit→result round-trip, a
right-censored result, and a cancel receipt with confirmed terminal state.
Run `make lint && make test`.
