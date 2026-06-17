from typing import Dict, Iterable, List

from .constants import (
    SEVERITY_ORDER,
    STATUS_APPROVED,
    STATUS_APPROVED_MINOR,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
)
from .models import ReviewIssue

# Keep root-cause issues before consequence issues. This makes validation and
# human review easier: for example, Question-Explanation Mismatch should appear
# before Key Mismatch when both are present.
ERROR_TYPE_ORDER = {
    "Question-Explanation Mismatch": 0,
    "Wrong Substitution": 1,
    "Formula Error": 2,
    "Missing Formula": 3,
    "Final Conclusion Mismatch": 3,
    "Blood Relation Ambiguity": 4,
    "Invalid Assumption": 5,
    "Ambiguity": 6,
    "Clarity Issue": 7,
    "Needs Human Review": 8,
    "Grammar / Formation Error": 9,
    "Question Wording / Label Mismatch": 10,
    "Answer Not in Options": 90,
    "Key Mismatch": 91,
    "Multiple Correct Options": 92,
}


def dedupe_issues(issues: Iterable[ReviewIssue]) -> List[ReviewIssue]:
    seen = set()
    result: List[ReviewIssue] = []
    for issue in issues:
        key = issue.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def sort_issues(issues: List[ReviewIssue]) -> List[ReviewIssue]:
    return sorted(
        issues,
        key=lambda item: (
            SEVERITY_ORDER.get(item.severity, 99),
            ERROR_TYPE_ORDER.get(item.error_type, 50),
            item.error_type,
            item.field,
        ),
    )


def status_from_issues(issues: List[ReviewIssue]) -> str:
    severities = {issue.severity for issue in issues}
    if "Critical" in severities:
        return STATUS_REJECTED
    if "Major" in severities:
        return STATUS_NEEDS_REVIEW
    if "Minor" in severities or "Suggestion" in severities:
        return STATUS_APPROVED_MINOR
    return STATUS_APPROVED


def combined_confidence(issues: List[ReviewIssue]) -> str:
    if not issues:
        return "High"
    values = {issue.confidence for issue in issues}
    if "Low" in values:
        return "Low"
    if "Medium" in values:
        return "Medium"
    return "High"


def reviewed_key_from_issues(issues: List[ReviewIssue]) -> str:
    for issue in issues:
        if issue.error_type == "Key Mismatch" and issue.suggested_fix.startswith("Option ") and issue.confidence == "High":
            return issue.suggested_fix
    return ""


def consolidate(issues: Iterable[ReviewIssue]) -> Dict[str, str]:
    clean_issues = sort_issues(dedupe_issues(issues))

    remarks = "\n".join(f"{idx}. {issue.to_remark()}" for idx, issue in enumerate(clean_issues, start=1))
    error_types = "; ".join(dict.fromkeys(issue.error_type for issue in clean_issues))
    fixes = "\n".join(
        f"{idx}. {issue.suggested_fix}"
        for idx, issue in enumerate(clean_issues, start=1)
        if issue.suggested_fix
    )

    return {
        "Agent Status": status_from_issues(clean_issues),
        "Error Count": str(len(clean_issues)),
        "Error Types": error_types,
        "Agent Remarks": remarks,
        "Suggested Corrections": fixes,
        "Confidence": combined_confidence(clean_issues),
        "Reviewed Key": reviewed_key_from_issues(clean_issues),
    }
