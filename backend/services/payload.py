"""Shared JSON-safe serialization for pandas values and DataFrame previews."""

from __future__ import annotations

from typing import Any

import pandas as pd


def json_value(value: Any) -> Any:
    """Convert pandas/numpy values into JSON-serializable Python values."""
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if hasattr(value, "item"):
        try:
            return json_value(value.item())
        except (ValueError, TypeError):
            pass
    if not isinstance(value, (dict, list, tuple)):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def frame_payload(frame: pd.DataFrame, limit: int = 500) -> dict[str, Any]:
    """Return a bounded, JSON-safe preview of a DataFrame."""
    view = frame.head(limit)
    return {
        "columns": [str(c) for c in view.columns],
        "rows": json_value(view.to_dict(orient="records")),
        "total_rows": len(frame),
        "truncated": len(frame) > limit,
    }
