import re
from typing import Dict, List

import pandas as pd

from .constants import REQUIRED_COLUMNS


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


COLUMN_ALIASES: Dict[str, str] = {
    "sno": "S. No",
    "slno": "S. No",
    "serialno": "S. No",
    "serialnumber": "S. No",
    "question": "Question",
    "questioncontent": "Question",
    "questiontext": "Question",
    "ques": "Question",
    "optiona": "Option A",
    "opta": "Option A",
    "a": "Option A",
    "optionb": "Option B",
    "optb": "Option B",
    "b": "Option B",
    "optionc": "Option C",
    "optc": "Option C",
    "c": "Option C",
    "optiond": "Option D",
    "optd": "Option D",
    "d": "Option D",
    "explanation": "Explanation",
    "solution": "Explanation",
    "key": "Key",
    "answerkey": "Key",
    "correctoption": "Key",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    used = set()

    for col in df.columns:
        canonical = COLUMN_ALIASES.get(_compact(col), col.strip())
        if canonical in used:
            # Avoid silently merging duplicate columns.
            canonical = col.strip()
        rename_map[col] = canonical
        used.add(canonical)

    return df.rename(columns=rename_map)


def missing_required_columns(df: pd.DataFrame) -> List[str]:
    return [col for col in REQUIRED_COLUMNS if col not in df.columns]


def cell_text(row, column: str) -> str:
    value = row.get(column, "")
    if value is None:
        return ""
    return str(value).strip()


def _normalize_excel_date_number(value: str) -> str:
    """Convert accidental Excel date coercions in option cells back to small integers.

    Some inputs contain an option intended as `24`, but Excel stores/displays it
    as `1900-01-24 00:00:00`. This is an input-normalization issue, not a
    content error. Keep this conservative to early-1900 date-looking values.
    """
    raw = str(value or "").strip()
    match = re.fullmatch(r"1900-01-(0?[1-9]|[12][0-9]|3[01])(?:\s+00:00:00)?", raw)
    if match:
        return str(int(match.group(1)))
    return raw


def normalize_input_values(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize cell values before review without changing the expected schema."""
    result = df.copy()
    for col in ["Option A", "Option B", "Option C", "Option D"]:
        if col in result.columns:
            result[col] = result[col].map(_normalize_excel_date_number)
    return result
