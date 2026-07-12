"""GPUHedge MOSS-TTS on Cerebrium — hardware-matched 48 GB L40S control vs Modal.

Deploy:  cd deploy/cerebrium && cerebrium deploy   (creates app "gpuhedge-moss")
Seed:    cerebrium run main.py::seed                (pre-load weights to storage)

The ``tts`` function becomes a POST endpoint; Cerebrium maps JSON body keys to
the parameters. Weights and encoded voice codes live on persistent storage so a
cold start does not re-download them (a Stage 1 pass criterion).

Secrets (set once with `cerebrium secrets`):
  GPUHEDGE_S3_BUCKET, GPUHEDGE_VOICE_PREFIX,
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
"""

# NOTE: no `from __future__ import annotations` here — Cerebrium's parameter
# validation introspects the live annotations and crashes on stringified ones
# ("isinstance() arg 2 must be a type...", observed 2026-07-11). Keep the
# signatures plain-typed.

import os

# Persistent storage is mounted here across runs -> no per-cold-start download.
os.environ.setdefault("HF_HOME", "/persistent-storage/hf")
os.environ.setdefault("MOSS_CACHE_DIR", "/persistent-storage/voices")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import moss_core  # noqa: E402


def tts(text: str, voice_id: str, voice_dir: str, language: str = "English") -> dict:
    """Short clone-conditioned TTS -> base64 WAV + cold-start metrics."""

    return moss_core.synthesize(
        text=text, voice_id=voice_id, voice_dir=voice_dir,
        language=language, tokenizer_on_gpu=True,
    )


def seed(voice_dir: str = "") -> dict:
    """Pre-load weights (and optionally pre-encode a voice) to persistent storage."""

    from huggingface_hub import snapshot_download

    snapshot_download(moss_core.MODEL_ID)
    seeded_voice = False
    if voice_dir:
        engine = moss_core.load_engine(tokenizer_on_gpu=True)
        moss_core.reference_codes(engine, voice_dir.rsplit("/", 1)[-1], voice_dir)
        seeded_voice = True
    return {"seeded_model": moss_core.MODEL_ID, "seeded_voice": seeded_voice}
