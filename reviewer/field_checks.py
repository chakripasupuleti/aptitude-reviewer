from typing import List

from .constants import OPTION_COLUMNS
from .models import ReviewIssue, make_issue
from .normalize import cell_text


def check_required_fields(row) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []

    if not cell_text(row, "Question"):
        issues.append(
            make_issue(
                "Critical",
                "Missing Field",
                "Question",
                "",
                "Question is missing.",
            )
        )

    for option_col in OPTION_COLUMNS:
        if not cell_text(row, option_col):
            issues.append(
                make_issue(
                    "Critical",
                    "Missing Field",
                    option_col,
                    "",
                    f"{option_col} is missing.",
                )
            )

    explanation = cell_text(row, "Explanation")
    if not explanation:
        issues.append(
            make_issue(
                "Major",
                "Missing Field",
                "Explanation",
                "",
                "Explanation is missing.",
            )
        )
    elif len(explanation.strip()) < 15:
        issues.append(
            make_issue(
                "Critical",
                "Missing Explanation",
                "Explanation",
                explanation.strip(),
                "Explanation is too short to justify the answer. A valid explanation must show the working.",
                confidence="High",
            )
        )

    if not cell_text(row, "Key"):
        issues.append(
            make_issue(
                "Critical",
                "Missing Field",
                "Key",
                "",
                "Key is missing.",
            )
        )

    return issues
