import re
from typing import List

from .models import ReviewIssue, make_issue
from .normalize import cell_text

TEXT_FIELDS = ["Question", "Explanation"]

_VERIFICATION_STEP_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"[Vv]erif(?:y|ying|ication)(?:\s+[Ss]tep)?"
    r"|[Ll]et'?s\s+verif[y]"
    r"|[Ll]et\s+me\s+verif[y]"
    r"|[Tt]o\s+verif[y]"
    r"|[Ww]e\s+can\s+verif[y]"
    r"|[Ll]et\s+us\s+verif[y]"
    r"|[Cc]ross\s*-?\s*[Cc]heck"
    r"|[Dd]ouble\s*-?\s*[Cc]heck"
    r")\s*:",
    re.IGNORECASE,
)


def _ordinal_suffix(number: int) -> str:
    if 10 <= number % 100 <= 20:
        return "th"
    last = number % 10
    if last == 1:
        return "st"
    if last == 2:
        return "nd"
    if last == 3:
        return "rd"
    return "th"


def check_grammar_basics(row) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []

    for field in TEXT_FIELDS:
        text = cell_text(row, field)
        if not text:
            continue

        for match in re.finditer(r"\b(\d+)(st|nd|rd|th)\b", text, flags=re.IGNORECASE):
            number = int(match.group(1))
            suffix = match.group(2).lower()
            expected = _ordinal_suffix(number)
            if suffix != expected:
                issues.append(
                    make_issue(
                        "Minor",
                        "Grammar / Formation Error",
                        field,
                        match.group(0),
                        f"Ordinal suffix is incorrect. Use {number}{expected}.",
                        suggested_fix=f"{number}{expected}",
                        confidence="High",
                    )
                )

        missing_space = re.search(r"(?<=[A-Za-z0-9])([.!?])(?=[A-Z])", text)
        if missing_space:
            start = max(0, missing_space.start() - 15)
            end = min(len(text), missing_space.end() + 15)
            snippet = text[start:end]
            issues.append(
                make_issue(
                    "Minor",
                    "Grammar / Formation Error",
                    field,
                    snippet,
                    "Missing space after punctuation.",
                    confidence="High",
                )
            )

        repeated = re.search(r"\b([A-Za-z]+)[ \t]+\1\b", text, flags=re.IGNORECASE)
        if repeated:
            issues.append(
                make_issue(
                    "Minor",
                    "Grammar / Formation Error",
                    field,
                    repeated.group(0),
                    "Repeated word found.",
                    confidence="High",
                )
            )
        singular_position = re.search(r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+position\b", text, flags=re.IGNORECASE)
        if singular_position:
            # "1 position" / "one position" is correctly singular — skip
            try:
                if int(singular_position.group(1)) <= 1:
                    singular_position = None
            except ValueError:
                pass  # word numbers (two, three…) are always plural
        if singular_position:
            issues.append(
                make_issue(
                    "Minor",
                    "Grammar / Formation Error",
                    field,
                    singular_position.group(0),
                    "Use plural wording for movement/count context, e.g., '5 positions' or '5 places'.",
                    suggested_fix=singular_position.group(0).replace(" position", " positions"),
                    confidence="High",
                )
            )

    return issues


def check_verification_step(row) -> List[ReviewIssue]:
    """Flag explicit verification/cross-check sections in explanations.

    AI-generated explanations often append a self-verification section.
    These are artifacts that should not appear in published explanations.
    """
    explanation = cell_text(row, "Explanation")
    if not explanation:
        return []
    match = _VERIFICATION_STEP_RE.search(explanation)
    if not match:
        return []
    snippet = match.group(0).strip()[:60]
    return [
        make_issue(
            "Major",
            "Verification Step",
            "Explanation",
            snippet,
            "The explanation contains a verification/cross-check section. "
            "This is an AI generation artifact and should not appear in the published explanation.",
            suggested_fix="Remove the verification section. The explanation should end at the final answer.",
            confidence="High",
        )
    ]
