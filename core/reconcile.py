"""File and text parsing for reconciling input values against database tables.

Supports CSV, TSV, TXT, JSON, and JSONL from either file uploads or direct paste.
Pure Python standard library only — no external dependencies.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

# Maximum number of unique values we allow in a single reconciliation to avoid
# exhausting server memory or timing out the query.
MAX_INPUT_VALUES = 50_000


class ReconcileParseError(Exception):
    """Raised when the uploaded file or pasted text cannot be parsed."""


@dataclass
class ParsedSource:
    """The result of parsing raw input text/file into a list of candidate values."""
    values: list[str]
    total_count: int
    unique_count: int
    format_detected: str
    column_name: str | None = None
    available_columns: list[str] = field(default_factory=list)


def _detect_delimiter(sample: str) -> str:
    """Guess whether the text is tab-separated or comma-separated."""
    # Count commas vs tabs in the first few lines
    first_lines = sample.splitlines()[:5]
    text = "\n".join(first_lines)
    tab_count = text.count("\t")
    comma_count = text.count(",")
    if tab_count > comma_count:
        return "\t"
    return ","


def parse_source(
    content: str | bytes,
    *,
    fmt: str = "auto",
    key: str | None = None,
    max_values: int = MAX_INPUT_VALUES,
) -> ParsedSource:
    """Parse text or bytes into a list of string values.

    Args:
        content: The raw string or bytes to parse.
        fmt: Format hint: 'auto', 'csv', 'tsv', 'txt', 'json', 'jsonl'.
        key: The column/key name to extract when parsing CSV/JSON/JSONL.
             If None for CSV, defaults to the first column.
        max_values: Safety cap on total raw values processed.

    Returns:
        ParsedSource with extracted values, counts, and column metadata.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1")
    else:
        text = content

    text = text.strip()
    if not text:
        return ParsedSource(
            values=[],
            total_count=0,
            unique_count=0,
            format_detected=fmt if fmt != "auto" else "txt",
        )

    # Auto-detect format if requested
    lines = [l for l in text.splitlines() if l.strip()]
    detected_fmt = fmt
    if detected_fmt == "auto":
        first_char = text[0]
        if first_char in ("[", "{"):
            detected_fmt = "json"
        elif len(lines) == 1 and "," in lines[0]:
            # Single line with commas (e.g. "1, 3, 10, 188, 94, 792387") is an inline list
            detected_fmt = "inline_list"
        elif "\t" in lines[0]:
            detected_fmt = "tsv"
        elif "," in lines[0] and len(lines) > 1:
            detected_fmt = "csv"
        elif lines[0].startswith("{") and lines[0].endswith("}"):
            detected_fmt = "jsonl"
        else:
            detected_fmt = "txt"

    raw_values: list[str] = []
    available_cols: list[str] = []
    selected_col: str | None = key

    if detected_fmt == "inline_list":
        parts = [p.strip() for p in text.replace("\t", ",").split(",") if p.strip()]
        for p in parts[:max_values]:
            raw_values.append(p)

    elif detected_fmt in ("csv", "tsv"):
        delim = "\t" if detected_fmt == "tsv" else _detect_delimiter(text[:2048])
        reader_rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        if not reader_rows:
            return ParsedSource(
                values=[],
                total_count=0,
                unique_count=0,
                format_detected=detected_fmt,
            )

        if len(reader_rows) == 1 and not key:
            # Only one line with multiple columns and no explicit key: treat all cells as values
            for val in reader_rows[0]:
                v = val.strip()
                if v:
                    raw_values.append(v)
        else:
            header = reader_rows[0]
            available_cols = [c.strip() for c in header if c.strip()]
            col_idx = 0
            if key and key in header:
                col_idx = header.index(key)
                selected_col = key
            elif available_cols:
                selected_col = available_cols[0]
                col_idx = header.index(selected_col)

            for i, row in enumerate(reader_rows[1:]):
                if i >= max_values:
                    break
                if len(row) > col_idx:
                    val = row[col_idx].strip()
                    if val:
                        raw_values.append(val)

    elif detected_fmt == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReconcileParseError(f"Invalid JSON: {exc}") from exc

        if isinstance(data, list):
            for i, item in enumerate(data):
                if i >= max_values:
                    break
                if isinstance(item, dict):
                    if not available_cols:
                        available_cols = list(item.keys())
                    target_key = key or (available_cols[0] if available_cols else None)
                    selected_col = target_key
                    if target_key and target_key in item:
                        v = str(item[target_key]).strip()
                        if v:
                            raw_values.append(v)
                elif item is not None:
                    v = str(item).strip()
                    if v:
                        raw_values.append(v)
        elif isinstance(data, dict):
            # If a dict is provided, check if it has a list field
            for k, val in data.items():
                if isinstance(val, list):
                    available_cols.append(k)
            target_key = key or (available_cols[0] if available_cols else None)
            selected_col = target_key
            if target_key and isinstance(data.get(target_key), list):
                for i, item in enumerate(data[target_key]):
                    if i >= max_values:
                        break
                    v = str(item).strip()
                    if v:
                        raw_values.append(v)
            else:
                raise ReconcileParseError(
                    "JSON must be an array of values, an array of objects, or contain a list field."
                )
        else:
            raise ReconcileParseError("JSON content must be an array or object.")

    elif detected_fmt == "jsonl":
        for i, line in enumerate(text.splitlines()):
            if i >= max_values:
                break
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip malformed line
            if isinstance(item, dict):
                if not available_cols:
                    available_cols = list(item.keys())
                target_key = key or (available_cols[0] if available_cols else None)
                selected_col = target_key
                if target_key and target_key in item:
                    v = str(item[target_key]).strip()
                    if v:
                        raw_values.append(v)
            elif item is not None:
                v = str(item).strip()
                if v:
                    raw_values.append(v)

    else:  # 'txt' or fallback
        detected_fmt = "txt"
        for i, line in enumerate(text.splitlines()):
            if len(raw_values) >= max_values:
                break
            val = line.strip()
            if not val:
                continue
            if "," in val:
                for sub in val.split(","):
                    s = sub.strip()
                    if s:
                        raw_values.append(s)
            else:
                raw_values.append(val)

    # Preserve order while finding unique elements
    seen = set()
    unique_values: list[str] = []
    for val in raw_values:
        if val not in seen:
            seen.add(val)
            unique_values.append(val)

    return ParsedSource(
        values=unique_values,
        total_count=len(raw_values),
        unique_count=len(unique_values),
        format_detected=detected_fmt,
        column_name=selected_col,
        available_columns=available_cols,
    )
