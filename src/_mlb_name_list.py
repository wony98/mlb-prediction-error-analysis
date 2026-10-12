# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Canonical parser for MLB lineup and bullpen name-list fields."""

from __future__ import annotations

import ast
import json
import math
import re
import warnings
from typing import Any, Iterable


_MALFORMED_BRACKET_COUNT = 0
_MALFORMED_BRACKET_SAMPLES: list[str] = []
_MAX_DIAGNOSTIC_SAMPLES = 8


def reset_parse_diagnostics() -> None:
    """Reset process-local parser diagnostics before a bounded validation pass."""
    global _MALFORMED_BRACKET_COUNT
    _MALFORMED_BRACKET_COUNT = 0
    _MALFORMED_BRACKET_SAMPLES.clear()


def get_parse_diagnostics() -> dict[str, Any]:
    """Return a copy of process-local malformed bracket diagnostics."""
    return {
        "malformed_bracket_count": _MALFORMED_BRACKET_COUNT,
        "malformed_bracket_samples": list(_MALFORMED_BRACKET_SAMPLES),
    }


def _record_malformed_bracket(text: str) -> None:
    global _MALFORMED_BRACKET_COUNT
    _MALFORMED_BRACKET_COUNT += 1
    if len(_MALFORMED_BRACKET_SAMPLES) < _MAX_DIAGNOSTIC_SAMPLES:
        _MALFORMED_BRACKET_SAMPLES.append(text[:240])
    warnings.warn(
        "malformed bracketed MLB name list; refusing delimiter fallback",
        RuntimeWarning,
        stacklevel=3,
    )


def _items_from_sequence(value: Iterable[Any]) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()]


def parse_name_list(
    value: Any,
    *,
    limit: int | None = None,
    dedupe: bool = False,
) -> list[str]:
    """Parse real sequences, JSON/Python list strings, or delimiter text."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, (list, tuple, set)):
        names = _items_from_sequence(value)
    else:
        text = str(value).strip()
        if not text or text.casefold() == "nan":
            return []
        parsed = None
        bracketed = text.startswith("[") and text.endswith("]")
        if bracketed:
            for parser in (json.loads, ast.literal_eval):
                try:
                    candidate = parser(text)
                except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                    continue
                if isinstance(candidate, (list, tuple, set)):
                    parsed = candidate
                    break
            if parsed is None:
                _record_malformed_bracket(text)
                return []
        names = (
            _items_from_sequence(parsed)
            if parsed is not None
            else [part.strip() for part in re.split(r"\s*[,;|/]\s*", text) if part.strip()]
        )

    if dedupe:
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            key = re.sub(r"[^a-z0-9]+", "", name.casefold())
            if key and key not in seen:
                unique.append(name)
                seen.add(key)
        names = unique
    return names[:limit] if limit is not None else names
