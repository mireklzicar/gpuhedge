"""Provider-agnostic MOSS-TTS-v1.5 engine shared by the three deploy recipes.

One short clone-conditioned request (~70 chars -> ~5 s audio) is the fixed
benchmark workload. The reference voice's audio codes are fetched once from S3
and cached on the platform's persistent storage, so a voice is encoded once
ever, not per request.

Config via environment (no project-specific names baked in):
  GPUHEDGE_S3_BUCKET        bucket holding the reference voice wavs
  GPUHEDGE_VOICE_PREFIX     key prefix (default "voices"); wav at
                            {prefix}/{voice_dir}/cloned.wav
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION
  MOSS_CACHE_DIR            where to cache encoded voice codes (persistent vol)

Kept dependency-light on purpose: transformers, soundfile, numpy, boto3,
hf_transfer. torch comes from each platform's base image (listing it — or
anything that hard-depends on it, like accelerate — bloats RunPod's bundle over
its 1.5 GB archive limit).
"""

from __future__ import annotations

import base64
import io
import math
import os
import re
import time
import wave
from pathlib import Path
from typing import Any

MODEL_ID = "OpenMOSS-Team/MOSS-TTS-v1.5"

_STATE: dict[str, Any] = {}


def install_transformers_moss_compat() -> None:
    """Bridge Transformers API renames used by the MOSS remote code."""

    import transformers.configuration_utils as config_utils
    import transformers.processing_utils as processing_utils
    from transformers import ProcessorMixin

    if not hasattr(processing_utils, "AUTO_TO_BASE_CLASS_MAPPING") and hasattr(
        processing_utils, "MODALITY_TO_BASE_CLASS_MAPPING"
    ):
        processing_utils.AUTO_TO_BASE_CLASS_MAPPING = (
            processing_utils.MODALITY_TO_BASE_CLASS_MAPPING
        )
    if not hasattr(processing_utils, "MODALITY_TO_BASE_CLASS_MAPPING") and hasattr(
        processing_utils, "AUTO_TO_BASE_CLASS_MAPPING"
    ):
        processing_utils.MODALITY_TO_BASE_CLASS_MAPPING = (
            processing_utils.AUTO_TO_BASE_CLASS_MAPPING
        )
    if not hasattr(config_utils, "PreTrainedConfig") and hasattr(config_utils, "PretrainedConfig"):
        config_utils.PreTrainedConfig = config_utils.PretrainedConfig
    processing_utils.MODALITY_TO_BASE_CLASS_MAPPING["audio_tokenizer"] = "PreTrainedModel"
    processing_utils.AUTO_TO_BASE_CLASS_MAPPING.setdefault("AutoModel", "PreTrainedModel")

    if getattr(ProcessorMixin, "_gpuhedge_moss_compat", False):
        return
    original_init = ProcessorMixin.__init__

    def patched_init(self, *args, **kwargs) -> None:
        if (
            "audio_tokenizer" in kwargs
            and "tokenizer" in kwargs
            and "feature_extractor" not in kwargs
            and getattr(self, "audio_tokenizer_class", None) == "AutoModel"
        ):
            self.attributes = ["tokenizer"]
        original_init(self, *args, **kwargs)

    ProcessorMixin.__init__ = patched_init
    ProcessorMixin._gpuhedge_moss_compat = True


def cache_dir() -> Path:
    return Path(os.environ.get("MOSS_CACHE_DIR", "/tmp/gpuhedge-cache"))


def load_engine(tokenizer_on_gpu: bool) -> dict[str, Any]:
    """Load MOSS once per process; report whether weights had to be downloaded."""

    if "engine" in _STATE:
        return _STATE["engine"]

    import torch
    from transformers import AutoModel, AutoProcessor

    started = time.perf_counter()
    hf_home = os.environ.get("HF_HOME", "")
    pre_existing = bool(hf_home) and any(Path(hf_home).glob("**/*MOSS*")) if hf_home else False

    install_transformers_moss_compat()
    processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
    if tokenizer_on_gpu:
        processor.audio_tokenizer = processor.audio_tokenizer.to("cuda")
    model = (
        AutoModel.from_pretrained(
            MODEL_ID, trust_remote_code=True,
            attn_implementation="sdpa", torch_dtype=torch.bfloat16,
        )
        .to("cuda")
        .eval()
    )
    _STATE["engine"] = {
        "torch": torch,
        "processor": processor,
        "model": model,
        "sr": int(processor.model_config.sampling_rate),
        "load_seconds": round(time.perf_counter() - started, 2),
        "loaded_at": time.time(),
        "gpu": torch.cuda.get_device_name(0),
        "downloaded_weights": not pre_existing,
        "codes": {},
    }
    return _STATE["engine"]


def reference_codes(engine: dict[str, Any], voice_id: str, voice_dir: str):
    import numpy as np
    import soundfile as sf

    torch = engine["torch"]
    if voice_id in engine["codes"]:
        return engine["codes"][voice_id]

    path = cache_dir() / "voices" / f"{voice_id}__cloned.npz"
    if path.exists():
        codes = torch.from_numpy(np.load(path)["codes"])
    else:
        import boto3

        bucket = os.environ["GPUHEDGE_S3_BUCKET"]
        prefix = os.environ.get("GPUHEDGE_VOICE_PREFIX", "voices")
        key = f"{prefix}/{voice_dir}/cloned.wav"
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        audio_np, file_sr = sf.read(io.BytesIO(body), dtype="float32", always_2d=True)
        if int(file_sr) != engine["sr"]:
            raise ValueError(f"reference sr {file_sr} != model {engine['sr']}")
        wav = torch.from_numpy(audio_np.T.copy())
        codes = engine["processor"].encode_audios_from_wav([wav], engine["sr"])[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, codes=codes.detach().to("cpu").numpy())
    engine["codes"][voice_id] = codes
    return codes


def estimate_new_tokens(text: str) -> int:
    words = len(re.findall(r"\b[\w']+\b", text))
    seconds = max(4.0, words / 2.55 + sum(text.count(m) for m in ".?!") * 0.08)
    return min(max(int(math.ceil(seconds * 28.0 * 1.2 / 128.0) * 128), 128), 8192)


def _wav_base64(samples, sr: int) -> str:
    import numpy as np

    pcm = (np.asarray(samples) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(sr)
        sink.writeframes(pcm.tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


def synthesize(
    text: str,
    voice_id: str,
    voice_dir: str,
    language: str = "English",
    *,
    tokenizer_on_gpu: bool,
) -> dict[str, Any]:
    """Short single-generation TTS -> base64 16-bit mono WAV + cold-start metrics."""

    request_started = time.perf_counter()
    already_loaded = "engine" in _STATE
    engine = load_engine(tokenizer_on_gpu)
    torch = engine["torch"]

    codes = reference_codes(engine, voice_id, voice_dir)
    gen_started = time.perf_counter()
    conversation = [
        engine["processor"].build_user_message(text=text, language=language, reference=[codes])
    ]
    batch = engine["processor"]([conversation], mode="generation")
    with torch.inference_mode():
        outputs = engine["model"].generate(
            input_ids=batch["input_ids"].to(engine["model"].device),
            attention_mask=batch["attention_mask"].to(engine["model"].device),
            max_new_tokens=estimate_new_tokens(text),
        )
    decoded = engine["processor"].decode(outputs)
    if not decoded or not decoded[0].audio_codes_list:
        raise RuntimeError("model returned no audio")
    audio = decoded[0].audio_codes_list[0].detach().to("cpu").float().flatten().clamp(-1.0, 1.0)
    gen_seconds = time.perf_counter() - gen_started

    return {
        "audio_base64": _wav_base64(audio.numpy(), engine["sr"]),
        "sample_rate": engine["sr"],
        "metrics": {
            "gpu": engine["gpu"],
            "already_loaded": already_loaded,
            "load_seconds": 0.0 if already_loaded else engine["load_seconds"],
            "downloaded_weights": engine["downloaded_weights"] and not already_loaded,
            "engine_age_seconds": round(time.time() - engine["loaded_at"], 1),
            "generation_seconds": round(gen_seconds, 2),
            "audio_seconds": round(len(audio) / engine["sr"], 2),
            "handler_seconds": round(time.perf_counter() - request_started, 2),
        },
    }
