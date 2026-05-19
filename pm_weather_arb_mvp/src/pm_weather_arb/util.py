from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, List, Sequence, TypeVar

T = TypeVar("T")


def jsonish(value: Any, default: Any = None) -> Any:
    """Gamma often returns JSON arrays as strings. Accept both native and string values."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return default
    return default


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        s = str(value).strip()
        if s.startswith("."):
            s = "0" + s
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def chunks(items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def norm_text(*parts: Any) -> str:
    raw = " ".join(str(p or "") for p in parts)
    return re.sub(r"\s+", " ", raw).strip().lower()


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def first_present(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def uniq(seq: Iterable[T]) -> List[T]:
    seen = set()
    out = []
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
