"""Result validators. The first HTTP 200 should not automatically win — the
first *valid* result does (docs/policies.md)."""

from __future__ import annotations

from gpuhedge.validators.audio import AudioValidation, validate_wav
from gpuhedge.validators.registry import (
    Validation,
    Validator,
    get_validator,
    register_validator,
)

__all__ = [
    "AudioValidation",
    "validate_wav",
    "Validation",
    "Validator",
    "get_validator",
    "register_validator",
]
