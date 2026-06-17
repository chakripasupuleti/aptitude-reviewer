from typing import List

from .constants import VALID_KEYS
from .models import ReviewIssue, make_issue
from .normalize import cell_text


def check_key_format(row) -> List[ReviewIssue]:
    key = row.get("Key", "")
    key_text = "" if key is None else str(key)

    if not key_text.strip():
        # Missing key is handled by field_checks.
        return []

    if key_text not in VALID_KEYS:
        return [
            make_issue(
                "Critical",
                "Invalid Key Format",
                "Key",
                key_text,
                "Key must be exactly one of: Option A, Option B, Option C, Option D.",
            )
        ]

    option_value = cell_text(row, key_text)
    if not option_value:
        return [
            make_issue(
                "Critical",
                "Invalid Key Mapping",
                "Key",
                key_text,
                f"Key is {key_text}, but {key_text} is empty.",
            )
        ]

    return []
