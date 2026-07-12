# Deploy the three MOSS-TTS endpoints (Stage 0)

GPUHedge is self-contained: it deploys its own MOSS-TTS-v1.5 endpoints on each
provider. The engine (`moss_core.py`) is shared; each provider dir holds a copy
because RunPod/Cerebrium bundle their directory and Modal imports it as a local
source. **`deploy/moss_core.py` is the source of truth** — after editing it, run
`make sync-core` (or `cp moss_core.py {modal,runpod,cerebrium}/`).

All three deployments:
- run the **same** MOSS-TTS-v1.5 commit and precision,
- pre-seed weights on persistent storage (no per-cold-start download),
- read one reference voice from S3 (`GPUHEDGE_S3_BUCKET` / `GPUHEDGE_VOICE_PREFIX`,
  wav at `{prefix}/{voice_dir}/cloned.wav`),
- return base64 WAV + cold-start metrics (`already_loaded`, `load_seconds`, …).

Modal & Cerebrium put the tokenizer on the 48 GB GPU; RunPod's 4090 keeps the
fp32 tokenizer on CPU (24 GB VRAM constraint). That asymmetry is deliberate and
labelled in the results.

## 0. One reference voice in S3
Upload a `cloned.wav` to `s3://$GPUHEDGE_S3_BUCKET/$GPUHEDGE_VOICE_PREFIX/<voice_dir>/cloned.wav`
matching `request.voice_dir` in `config/benchmark.yaml`.

## 1. Modal — `gpuhedge-moss` (L40S)
```bash
modal secret create gpuhedge-aws \
  GPUHEDGE_S3_BUCKET=... GPUHEDGE_VOICE_PREFIX=voices \
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=...
modal deploy deploy/modal/app.py
modal run   deploy/modal/app.py::smoke      # optional
```
Then set `providers.modal.deployed: true` in `config/benchmark.yaml`.

## 2. RunPod Flash — `moss4090` (RTX 4090, EU-RO-1 volume)
```bash
cp deploy/runpod/.env.example deploy/runpod/.env   # fill S3 + AWS creds
cd deploy/runpod && flash deploy
```
Copy the printed **endpoint id** into `providers.runpod.endpoint_id` and set
`providers.runpod.deployed: true`. First-ever call downloads ~17 GB to the
`gpuhedge-hf` network volume; subsequent cold starts read from it.

## 3. Cerebrium — `gpuhedge-moss` (L40S) — Stage 1 qualifies this
```bash
cerebrium secrets set GPUHEDGE_S3_BUCKET ...   # + GPUHEDGE_VOICE_PREFIX, AWS_*
cd deploy/cerebrium
cerebrium deploy
cerebrium run main.py::seed                     # pre-load weights to storage
```
Then set `providers.cerebrium.deployed: true` and run `gpuhedge qualify`.

**Verify at deploy:** the L40S enum in `cerebrium.toml` (`compute = "ADA_L40S"`;
fall back to `"ADA_L40"` if rejected) and the async-run / cancel REST paths that
`CerebriumBackend` marks `VERIFY-IN-STAGE-1`.
