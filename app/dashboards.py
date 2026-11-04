"""Temporary dashboards the assistant makes to show data at a glance: headline numbers, charts
and tables, drawn by the web UI (app/web/app.js, renderDashboard) under the message.

A dashboard is a small JSON spec, checked and cleaned here, never HTML or code: the UI builds
every element itself, so a spec (which may carry text from web pages or mail) can't run
anything. It lives with the chat message it belongs to (encrypted with the chat in private
chats) and goes when the chat is deleted; it isn't a saved file unless the person asks for one.

    {"title": "Q3 sales", "subtitle": "From sales.csv",
     "kpis":   [{"label": "Revenue", "value": "₹4.2 L", "change": "+12% vs Q2", "trend": "up"}],
     "charts": [{"type": "bar" | "hbar" | "line", "title": "...", "labels": ["Jul", "Aug"],
                 "series": [{"name": "Revenue", "values": [1.2, 1.5]}], "unit": "₹ lakh"}],
     "tables": [{"title": "...", "columns": ["Region", "Revenue"], "rows": [["North", 1.2]]}],
     "notes":  "Markdown text: what stands out, caveats, sources."}
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

MAX_KPIS = 8
MAX_CHARTS = 6
MAX_SERIES = 8      # categorical colours: past this, fold into "Other" (the model is told)
MAX_POINTS = 60
MAX_TABLES = 4
MAX_ROWS = 200
MAX_COLUMNS = 12
MAX_TEXT = 200
MAX_NOTES = 3000
MAX_SPEC_CHARS = 60_000
CHART_TYPES = {"bar", "hbar", "line"}
# Words a model may use for a chart type, mapped onto the three the UI draws. A pie becomes a
# sorted horizontal bar: easier to read and compare.
CHART_ALIASES = {"column": "bar", "columns": "bar", "barh": "hbar", "horizontal_bar": "hbar", "pie": "hbar",
                 "donut": "hbar", "doughnut": "hbar", "area": "line", "lines": "line", "trend": "line"}


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.split())[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("₹", "").replace("$", "").replace("%", "")
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _cell(value: Any) -> str | float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return _text(value, 300)


def _list(spec: dict, key: str) -> list:
    value = spec.get(key) or []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list")
    return value


def _chart(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"chart {index + 1} must be an object")
    kind = _text(raw.get("type") or "bar").lower()
    kind = CHART_ALIASES.get(kind, kind)
    if kind not in CHART_TYPES:
        raise ValueError(f"chart {index + 1}: type must be bar, hbar or line")
    labels = [_text(label, 60) for label in (raw.get("labels") or [])][:MAX_POINTS]
    series_raw = raw.get("series")
    if series_raw is None and "values" in raw:  # one series written flat
        series_raw = [{"name": raw.get("title") or "Value", "values": raw["values"]}]
    if isinstance(series_raw, dict):
        series_raw = [series_raw]
    if not isinstance(series_raw, list) or not series_raw:
        raise ValueError(f"chart {index + 1}: give 'series' as a list of {{name, values}}")
    if len(series_raw) > MAX_SERIES:
        raise ValueError(f"chart {index + 1}: at most {MAX_SERIES} series; fold the smallest into 'Other'")
    series = []
    for s in series_raw:
        if not isinstance(s, dict) or not isinstance(s.get("values"), list):
            raise ValueError(f"chart {index + 1}: each series needs a name and a list of values")
        values = [_number(v) for v in s["values"]][:MAX_POINTS]
        series.append({"name": _text(s.get("name") or f"Series {len(series) + 1}", 60), "values": values})
    points = max(len(s["values"]) for s in series)
    if not labels:
        labels = [str(i + 1) for i in range(points)]
    if points != len(labels):
        raise ValueError(f"chart {index + 1}: {len(labels)} labels but {points} values; give one value per label")
    if all(v is None for s in series for v in s["values"]):
        raise ValueError(f"chart {index + 1}: no numeric values")
    return {"type": kind, "title": _text(raw.get("title")), "labels": labels, "series": series,
            "unit": _text(raw.get("unit"), 30), "pie": _text(raw.get("type")).lower() in ("pie", "donut", "doughnut")}


def _table(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"table {index + 1} must be an object")
    columns = [_text(c, 60) for c in (raw.get("columns") or [])][:MAX_COLUMNS]
    rows_raw = raw.get("rows") or []
    if not isinstance(rows_raw, list):
        raise ValueError(f"table {index + 1}: rows must be a list of lists")
    rows = []
    for row in rows_raw[:MAX_ROWS]:
        if isinstance(row, dict):
            if not columns:
                columns = [_text(c, 60) for c in row][:MAX_COLUMNS]
            row = [row.get(c) for c in columns]
        if not isinstance(row, list):
            raise ValueError(f"table {index + 1}: each row must be a list")
        rows.append([_cell(v) for v in row[:MAX_COLUMNS]])
    if not columns and rows:
        columns = [f"Column {i + 1}" for i in range(len(rows[0]))]
    return {"title": _text(raw.get("title")), "columns": columns, "rows": rows,
            "more": max(0, len(rows_raw) - MAX_ROWS)}


def normalize(spec: Any) -> dict:
    """A clean dashboard from what the model (or its code) gave. Raises ValueError with a
    message the model can act on."""
    if isinstance(spec, str):
        if len(spec) > MAX_SPEC_CHARS:
            raise ValueError("the dashboard is too big: summarise the data first (top rows, totals)")
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as e:
            raise ValueError(f"the dashboard is not valid JSON ({e.msg} at character {e.pos})") from None
    if not isinstance(spec, dict):
        raise ValueError("the dashboard must be a JSON object")
    kpis = []
    for k in _list(spec, "kpis")[:MAX_KPIS]:
        if not isinstance(k, dict):
            raise ValueError("each kpi must be an object with label and value")
        trend = _text(k.get("trend")).lower()
        kpis.append({"label": _text(k.get("label"), 60), "value": _text(k.get("value"), 40),
                     "change": _text(k.get("change") or k.get("note"), 80),
                     "trend": trend if trend in ("up", "down", "flat") else ""})
    charts = [_chart(c, i) for i, c in enumerate(_list(spec, "charts")[:MAX_CHARTS])]
    tables = [_table(t, i) for i, t in enumerate(_list(spec, "tables")[:MAX_TABLES])]
    notes = str(spec.get("notes") or "")[:MAX_NOTES]
    if not (kpis or charts or tables or notes.strip()):
        raise ValueError("the dashboard is empty: give kpis, charts or tables")
    return {
        "id": uuid.uuid4().hex[:12], "title": _text(spec.get("title") or "Dashboard", 120),
        "subtitle": _text(spec.get("subtitle"), 200), "kpis": kpis, "charts": charts, "tables": tables,
        "notes": notes,
    }


def describe(dash: dict) -> str:
    """What the model is told after making one (short: it must not repeat the data)."""
    parts = [f"{len(dash['kpis'])} headline numbers" if dash["kpis"] else "",
             f"{len(dash['charts'])} chart(s)" if dash["charts"] else "",
             f"{len(dash['tables'])} table(s)" if dash["tables"] else ""]
    what = ", ".join(p for p in parts if p) or "notes"
    return (f"Dashboard '{dash['title']}' ({what}) is shown to the person under your reply. Don't repeat its "
            "numbers or tables in your reply: give the two or three takeaways in a few short lines, and "
            "mention anything you couldn't include.")
