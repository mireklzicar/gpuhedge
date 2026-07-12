"""WAV validation gate for the TTS race.

A provider that returns malformed audio fast must not win over one still
producing valid audio. Validation requires a parseable WAV with the expected
sample rate and channels, a plausible duration, nonzero RMS, and no NaNs or a
clipping catastrophe (benchmarks/2026-07-moss/methodology.md, docs/policies.md).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np


@dataclass
class AudioValidation:
    valid: bool
    reasons: list[str] = field(default_factory=list)
    sample_rate: int | None = None
    channels: int | None = None
    duration_s: float | None = None
    rms: float | None = None
    peak: float | None = None
    clipped_fraction: float | None = None

    def __bool__(self) -> bool:  # so callers can `if validate_wav(...):`
        return self.valid


def validate_wav(
    data: bytes | None,
    *,
    expected_sample_rate: int | None = None,
    min_duration_s: float = 0.5,
    max_duration_s: float = 120.0,
    min_rms: float = 1e-4,
    max_clipped_fraction: float = 0.01,
    allowed_channels: tuple[int, ...] = (1, 2),
) -> AudioValidation:
    """Validate decoded WAV bytes. Never raises — a bad result is a failed
    validation, not an exception, so the race keeps waiting for the other arm."""

    reasons: list[str] = []
    if not data:
        return AudioValidation(False, ["empty result"])

    try:
        import soundfile as sf

        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        return AudioValidation(False, [f"unparseable WAV: {exc}"])

    channels = int(audio.shape[1])
    duration_s = float(audio.shape[0]) / sr if sr else 0.0
    mono = audio.mean(axis=1)

    has_nan = bool(np.isnan(audio).any())
    rms = 0.0 if has_nan else float(np.sqrt(np.mean(np.square(mono))))
    peak = 0.0 if has_nan else float(np.max(np.abs(mono)))
    clipped_fraction = 0.0 if has_nan else float(np.mean(np.abs(mono) >= 0.999))

    if has_nan:
        reasons.append("contains NaNs")
    if channels not in allowed_channels:
        reasons.append(f"unexpected channels {channels} (allowed {allowed_channels})")
    if expected_sample_rate is not None and sr != expected_sample_rate:
        reasons.append(f"sample rate {sr} != expected {expected_sample_rate}")
    if not (min_duration_s <= duration_s <= max_duration_s):
        reasons.append(f"duration {duration_s:.2f}s outside [{min_duration_s}, {max_duration_s}]")
    if rms < min_rms:
        reasons.append(f"near-silent: rms {rms:.2e} < {min_rms:.0e}")
    if clipped_fraction > max_clipped_fraction:
        reasons.append(f"clipping: {clipped_fraction:.1%} of samples at rail")

    return AudioValidation(
        valid=not reasons,
        reasons=reasons,
        sample_rate=int(sr),
        channels=channels,
        duration_s=round(duration_s, 3),
        rms=round(rms, 6),
        peak=round(peak, 6),
        clipped_fraction=round(clipped_fraction, 6),
    )
