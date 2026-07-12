"""GPUHedge MOSS-TTS on Modal — the 48 GB L40S hedge arm.

Deploy:  modal deploy deploy/modal/app.py         (creates app "gpuhedge-moss")
Smoke:   modal run   deploy/modal/app.py::smoke

Short ``scaledown_window`` on purpose: the benchmark forces a genuine cold start
every round by waiting past scale-down, so we want fast scale-down here (a
production deployment would use a long window instead). The fp32 audio tokenizer
lives on the GPU (48 GB), unlike the 4090 arm.

Requires a Modal secret ``gpuhedge-aws`` with AWS creds + GPUHEDGE_S3_BUCKET
(and optional GPUHEDGE_VOICE_PREFIX) for the reference voice.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import modal

APP_NAME = "gpuhedge-moss"
GPU = "L40S"
CACHE = "/cache"
SCALEDOWN_WINDOW = int(os.environ.get("GPUHEDGE_MODAL_SCALEDOWN", "20"))

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch==2.11.0", "torchaudio==2.11.0", "transformers==5.9.0",
        "accelerate==1.12.0", "soundfile==0.13.1", "numpy==2.3.5",
        "boto3~=1.40", "hf_transfer",
    )
    .env({
        "HF_HOME": f"{CACHE}/hf",
        "HF_HUB_ENABLE_HF_TRANSFER": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "MOSS_CACHE_DIR": f"{CACHE}/voices",
    })
    .add_local_python_source("moss_core")
)

cache_vol = modal.Volume.from_name("gpuhedge-hf-cache", create_if_missing=True)


@app.cls(
    image=image,
    gpu=GPU,
    volumes={CACHE: cache_vol},
    secrets=[modal.Secret.from_name("gpuhedge-aws")],
    timeout=3600,
    scaledown_window=SCALEDOWN_WINDOW,
    memory=32768,
    cpu=8,
)
class MossTTS:
    @modal.enter()
    def load(self) -> None:
        import moss_core

        self.core = moss_core
        self.engine = moss_core.load_engine(tokenizer_on_gpu=True)
        cache_vol.commit()

    @modal.method()
    def tts(
        self, text: str, voice_id: str, voice_dir: str,
        language: str = "English", reference: str = "cloned",
    ) -> dict[str, Any]:
        result = self.core.synthesize(
            text, voice_id, voice_dir, language, tokenizer_on_gpu=True
        )
        # Adapter expects raw WAV bytes under "audio".
        return {
            "audio": base64.b64decode(result["audio_base64"]),
            "sample_rate": result["sample_rate"],
            "metrics": result["metrics"],
        }


_SMOKE_VOICE_DIR = (
    "english/american/"
    "english_american_05_male_senior_hifi_english_american_male_speaker_005"
)


@app.local_entrypoint()
def smoke(voice_dir: str = _SMOKE_VOICE_DIR) -> None:
    voice_id = voice_dir.rsplit("/", 1)[-1]
    out = MossTTS().tts.remote(
        text="A quick generation to measure cold start across serverless GPU clouds.",
        voice_id=voice_id, voice_dir=voice_dir,
    )
    print(out["metrics"])
