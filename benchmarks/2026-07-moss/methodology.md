# Methodology — 2026-07 three-provider MOSS-TTS cold-start benchmark

## Question

If the same model is deployed identically on multiple serverless GPU clouds,
how are cold-start latencies distributed, how correlated are the tails, and
can a hedging policy exploit the difference at acceptable cost?

## Workload

- **Model**: MOSS-TTS-v1.5 (17 GB bf16 LM + fp32 audio tokenizer), same
  model commit on every provider.
- **Request**: one fixed ~77-character text → ~5–7 s of audio, voice-cloned
  from a cached S3 reference; identical across providers and rounds.
- **Validation**: parseable WAV, plausible duration, nonzero RMS, no
  NaN/clipping. A fast malformed response does not count as success.

## Arms

| arm | GPU | notes |
| --- | --- | --- |
| RunPod serverless (Flash) | RTX 4090 24 GB | FlashBoot on; audio tokenizer on CPU (24 GB can't hold it beside the LM) — labelled asymmetry |
| Modal | L40S 48 GB | tokenizer on GPU |
| Cerebrium | L40S 48 GB | tokenizer on GPU; hardware-matched control vs Modal |

Weights pre-seeded to each provider's persistent storage (no weight
downloads during measurement; verified per round from worker metrics).

## Procedure

- **Stage 1 — qualification**: the least-documented provider had to pass
  provisioning (≥5/6 containers within 5 min), valid output, no weight
  re-download, and cancellation-to-terminal gates before the benchmark
  committed to it (max $4).
- **Stage 2 — paired cold rounds**: 9 blocks × 6 rounds = 54 rounds. Each
  round submits the same request to all three providers concurrently, lets
  every arm finish (300 s right-censor cap), then issues a warm companion
  request to the same container to separate infrastructure delay from
  generation time. ~130 s scale-to-zero waits between rounds (all providers
  idle-timeout well below that).
- **Stage 3 — live hedging**: 18 requests through the real hedging engine
  (6× runpod→modal@10 s, 6× runpod→cerebrium@10 s, 6× adaptive), with real
  loser cancellation and receipts.
- **Splits**: static policy parameters were frozen after rounds 1–18
  (calibration). Rounds 19–54 are the **evaluation set** — *not* a strict
  holdout, since all 54 rounds were examined during analysis. Policies
  discovered post hoc (the queue cutover) get their own pre-registered
  validation: `../2026-07-queue-cutover/`.

## Measurement details

- Latency = client-observed wall time from submit to valid result; hedge
  wins report end-to-end from *request* start (the hedge's launch offset is
  included).
- Provider-reported metrics captured per job where available (queue delay,
  billed execution time, load/generation seconds, GPU model).
- Costs tracked at three layers: projected ledger with hard budget gates;
  provider-reported actuals at block boundaries; two replay cost models
  (active-compute and idle-inclusive billed) — see
  [../../docs/cost-accounting.md](../../docs/cost-accounting.md).
- Reporting: p50/p95/empirical max and fixed-deadline miss *counts* with
  Wilson 95% intervals. No p99s from tens of samples.

## Known limitations

- One workload, one region per provider, ~7 h collection window on one
  evening (Fri→Sat UTC) at a steady ~2.5 min cadence. FlashBoot hit rates
  are cadence-dependent (an earlier idle-heavy probe saw 2/7 vs 36/54 here).
- Round 13 lost its Cerebrium arm to a session-token expiry; round 49's
  Cerebrium sample was warm-contaminated (block chained without a cooldown
  gap). Both visible in the committed traces.
- The GPU asymmetry (24 GB primary vs 48 GB hedges) is a deliberate
  cost-tier choice, not a hardware comparison.

## Reproducing

```bash
gpuhedge replay traces/moss_rounds.jsonl          # all tables
python benchmarks/2026-07-moss/analysis.py        # all figures
```
