import re
from typing import List, Tuple

from .models import ReviewIssue, make_issue
from .normalize import cell_text

TEXT_FIELDS = [
    "Question",
    "Option A",
    "Option B",
    "Option C",
    "Option D",
    "Explanation",
]

LATEX_PATTERNS = [
    r"\\d?frac\s*\{[^{}]+\}\s*\{[^{}]+\}",
    r"\\tfrac\s*\{[^{}]+\}\s*\{[^{}]+\}",
    r"\\times\b",
    r"\\div\b",
    r"\\sqrt\s*\{[^{}]+\}",
    r"\^\\circ",
]


def _content_is_math_like(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if re.search(r"\\[A-Za-z]+|[+*/=^{}<>≤≥≡≠]|%|°", stripped):
        return True
    if re.fullmatch(r"[0-9.,\s/()-]+", stripped):
        return True
    # Algebraic variable expressions like $5k$, $7k$, $3x$, $2n$.
    if re.search(r"\d[a-zA-Z]|[a-zA-Z]\d", stripped):
        return True
    # Single-letter math variables: $N$, $k$, $x$
    if re.fullmatch(r"[a-zA-Z]", stripped):
        return True
    return False


def math_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    start = None
    for index, char in enumerate(text):
        if char != "$" or (index > 0 and text[index - 1] == "\\"):
            continue
        if start is None:
            start = index
        else:
            if _content_is_math_like(text[start + 1:index]):
                spans.append((start, index))
            start = None
    return spans


def is_inside_spans(index: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in spans)


def _has_unbalanced_math_dollar(text: str) -> bool:
    """Return True when dollar signs that look like math delimiters are unbalanced.

    Uses math_spans to identify already-balanced $...$ pairs first.
    Any remaining $ that is not a plain currency amount ($800, $1,500) is an
    orphan math delimiter; an odd orphan count means unbalanced.
    """
    balanced: set = set()
    for start, end in math_spans(text):
        balanced.add(start)
        balanced.add(end)

    orphan_count = 0
    for m in re.finditer(r"(?<!\\)\$", text):
        idx = m.start()
        if idx in balanced:
            continue  # Part of a valid balanced math span
        # Plain currency: $800, $1,500 (digits only, no trailing math character)
        pos = m.end()
        num_m = re.match(r"[\d,.]+", text[pos:])
        if num_m:
            after_pos = pos + len(num_m.group(0))
            if after_pos >= len(text) or not text[after_pos].isalpha():
                continue
        orphan_count += 1
    return orphan_count % 2 != 0


def check_latex_formatting(row) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []

    for field in TEXT_FIELDS:
        text = cell_text(row, field)
        if not text:
            continue

        if _has_unbalanced_math_dollar(text):
            issues.append(
                make_issue(
                    "Minor",
                    "LaTeX Formatting",
                    field,
                    text,
                    "Dollar delimiters appear to be unbalanced around a math expression. LaTeX expressions should use matching $...$ delimiters.",
                )
            )


        spans = math_spans(text)
        for pattern in LATEX_PATTERNS:
            for match in re.finditer(pattern, text):
                if not is_inside_spans(match.start(), spans):
                    issues.append(
                        make_issue(
                            "Major",
                            "LaTeX Formatting",
                            field,
                            match.group(0),
                            "LaTeX command is not wrapped in $...$.",
                        )
                    )
                    break

    return issues
