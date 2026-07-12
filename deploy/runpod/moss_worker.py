"""GPUHedge MOSS-TTS on RunPod Flash — the cheap/fast RTX 4090 primary arm.

Deploy: cd deploy/runpod && flash deploy      (creates endpoint "moss4090")
Then copy the printed endpoint id into config/benchmark.yaml
(providers.runpod.endpoint_id) and set providers.runpod.deployed: true.

The 4090's 24 GB VRAM can't hold the fp32 audio tokenizer beside the bf16 LM, so
the tokenizer stays on CPU (tokenizer_on_gpu=False). Weights cache on the
network volume "gpuhedge-hf"; the queue handler maps the JSON ``{"data": {...}}``
input to kwargs.

SDK gotchas honoured (runpod-flash): deps come from requirements.txt (a shared
``**_COMMON`` dict yields "0 deps"); ``accelerate``/torch are NOT listed (they
blow past the 1.5 GB archive limit — torch is in the base image); callers use
``run()`` + ``wait()`` because ``/runsync`` caps ~90 s.
"""

from __future__ import annotations

import os
from pathlib import Path

import moss_core
from runpod_flash import Endpoint, GpuType, NetworkVolume

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

VOLUME = NetworkVolume(name="gpuhedge-hf", size=50)

ENV = {
    "HF_HOME": "/runpod-volume/hf",
    "HF_HUB_ENABLE_HF_TRANSFER": "1",
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    "MOSS_CACHE_DIR": "/runpod-volume/voices",
    "GPUHEDGE_S3_BUCKET": os.environ.get("GPUHEDGE_S3_BUCKET", ""),
    "GPUHEDGE_VOICE_PREFIX": os.environ.get("GPUHEDGE_VOICE_PREFIX", "voices"),
    "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
    "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
    "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
}

_COMMON = dict(
    workers=(0, 1),
    # 5 s (was 60 s for Stage 2): the post-job idle window bills at the GPU
    # rate, and the 2026-07 queue-cutover validation measures account-billed
    # cost per block — the practical-minimum idle keeps balance deltas about
    # execution, not idle. Stage 2 traces/tables assume the old 60 s value
    # (config providers.runpod.idle_billed_seconds).
    idle_timeout=5,
    env=ENV,
    volume=VOLUME,
    execution_timeout_ms=600_000,
)


@Endpoint(name="moss4090", gpu=GpuType.NVIDIA_GEFORCE_RTX_4090, flashboot=True, **_COMMON)
async def moss4090(data: dict) -> dict:
    return moss_core.synthesize(
        text=data["text"],
        voice_id=data["voice_id"],
        voice_dir=data["voice_dir"],
        language=data.get("language", "English"),
        tokenizer_on_gpu=False,
    )
