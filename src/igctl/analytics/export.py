"""Write any report (dict / list of dicts / pydantic models) to JSON or CSV."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from igctl.backends.base import IgctlError

FORMATS = ("json", "csv")


def to_plain(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, dict):
        return {str(k): to_plain(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [to_plain(v) for v in data]
    return data


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Nested dicts/lists to a single-level dict with dotted keys (``a.0.b``)."""
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = ((str(i), v) for i, v in enumerate(data))
    else:
        return {prefix or "value": data}
    for k, v in items:
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict | list) and v:
            out.update(flatten(v, key))
        else:
            out[key] = None if isinstance(v, dict | list) else v
    return out


def csv_rows(data: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Header and rows for CSV.

    A list of records becomes one row per record (nested fields flattened); a dict that
    holds a ``rows`` list uses those; any other dict becomes ``section,key,value`` rows.
    """
    data = to_plain(data)
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        data = data["rows"]
    if isinstance(data, list):
        rows = [flatten(r) if isinstance(r, dict) else {"value": r} for r in data]
        header: list[str] = []
        for r in rows:
            header.extend(k for k in r if k not in header)
        return header, rows
    if isinstance(data, dict):
        rows = []
        for section, value in data.items():
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                for i, item in enumerate(value):
                    for k, v in flatten(item).items():
                        rows.append({"section": section, "key": f"{i}.{k}", "value": v})
            elif isinstance(value, dict):
                for k, v in flatten(value).items():
                    rows.append({"section": section, "key": k, "value": v})
            else:
                rows.append({"section": "", "key": section, "value": value})
        return ["section", "key", "value"], rows
    return ["value"], [{"value": data}]


def export(data: Any, path: str | Path, fmt: str | None = None) -> Path:
    """Write ``data`` to ``path``; format comes from ``fmt`` or the file extension."""
    path = Path(path)
    fmt = (fmt or path.suffix.lstrip(".") or "json").lower()
    if fmt not in FORMATS:
        raise IgctlError(f"Unsupported export format '{fmt}' (use .json or .csv).")
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(to_plain(data), indent=2, default=str), encoding="utf-8")
        return path
    header, rows = csv_rows(data)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path
