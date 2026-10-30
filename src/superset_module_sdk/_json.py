"""Strict JSON and topic-segment helpers shared by the SDK."""

from __future__ import annotations

import json
import math
import unicodedata
from typing import Any, NoReturn

MAX_SEGMENT_BYTES = 200


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e400 parses to infinity
        raise ValueError("number is not finite")
    return value


def _finite_int(text: str) -> int:
    value = int(text)
    try:
        float(value)
    except OverflowError:  # beyond the double range: not finite for the other SDKs
        raise ValueError("number is not finite") from None
    return value


def parse_json(data: str | bytes | bytearray) -> Any:
    """JSON with every number finite as an IEEE-754 double: no NaN, no Infinity, nothing beyond
    the double range. A value the module passes on must stay valid JSON everywhere.

    Bytes must be strict UTF-8, as in the other SDKs: UTF-16 and UTF-32, encoded surrogates and a
    leading byte order mark are refused (json.loads would detect and accept them from bytes).
    UnicodeDecodeError is a ValueError."""
    text = data if isinstance(data, str) else bytes(data).decode("utf-8")
    return json.loads(
        text, parse_constant=_reject_constant, parse_float=_finite_float, parse_int=_finite_int
    )


def dump_json(value: Any) -> str:
    """Compact JSON; NaN and Infinity are refused (ValueError)."""
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def valid_segment(value: object) -> bool:
    """A topic level or id: non-empty, at most 200 bytes, no control character, /, + or #."""
    return (
        isinstance(value, str)
        and 0 < len(value.encode()) <= MAX_SEGMENT_BYTES
        and not any(c in "/+#" or unicodedata.category(c) == "Cc" for c in value)
    )


def is_int(value: object) -> bool:
    """An integer that is not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)
