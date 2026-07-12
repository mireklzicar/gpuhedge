"""Pluggable result validators.

The race winner is the first *valid* result, so "valid" must be definable per
workload: WAV gates for TTS, JSON for LLM endpoints, anything callable for
custom pipelines. Configs select a validator by name
(``request.validator: wav``); libraries register their own:

    from gpuhedge.validators import register_validator

    @register_validator("my-schema")
    def check(result):  # ProviderResult -> Validation
        ...
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gpuhedge.validators.audio import validate_wav

if TYPE_CHECKING:  # pragma: no cover
    from gpuhedge.backends.base import ProviderResult
    from gpuhedge.config import BenchmarkConfig


@dataclass
class Validation:
    valid: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.valid


Validator = Callable[["ProviderResult"], Validation]

_VALIDATORS: dict[str, Validator] = {}


def register_validator(name: str) -> Callable[[Validator], Validator]:
    def wrap(fn: Validator) -> Validator:
        _VALIDATORS[name] = fn
        return fn

    return wrap


def get_validator(name_or_config: str | BenchmarkConfig = "wav") -> Validator:
    """Resolve by name, or from ``config.request.validator`` (default: wav)."""

    if not isinstance(name_or_config, str):
        name = str(name_or_config.request.get("validator", "wav"))
    else:
        name = name_or_config
    try:
        return _VALIDATORS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown validator {name!r}; registered: {sorted(_VALIDATORS)}"
        ) from exc


@register_validator("wav")
def _wav(result: ProviderResult) -> Validation:
    v = validate_wav(result.audio)
    return Validation(v.valid, v.reasons, {
        "duration_s": v.duration_s, "sample_rate": v.sample_rate, "rms": v.rms,
    })


@register_validator("json")
def _json(result: ProviderResult) -> Validation:
    data = result.audio  # the adapters carry the raw payload here
    if not data:
        return Validation(False, ["empty result"])
    try:
        json.loads(data)
    except Exception as exc:  # noqa: BLE001
        return Validation(False, [f"unparseable JSON: {exc}"])
    return Validation(True)


@register_validator("nonempty")
def _nonempty(result: ProviderResult) -> Validation:
    return (Validation(True) if result.audio
            else Validation(False, ["empty result"]))
