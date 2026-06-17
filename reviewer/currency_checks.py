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


def _content_is_math_like(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    # LaTeX commands/operators clearly indicate math mode.
    if re.search(r"\\[A-Za-z]+|[+*/=^{}<>≤≥≡≠]|%|°", stripped):
        return True
    # Numeric/fraction-only content like $30/11$ or $100$.
    if re.fullmatch(r"[0-9.,\s/()-]+", stripped):
        return True
    # Algebraic variable expressions like $5k$, $7k$, $3x$, $2n$ (age/algebra problems).
    if re.search(r"\d[a-zA-Z]|[a-zA-Z]\d", stripped):
        return True
    return False


def _math_dollar_spans(text: str) -> List[Tuple[int, int]]:
    """Return balanced $...$ spans that look like math delimiters.

    This avoids treating real currency text such as "$100 and ... $120" as one
    large math span, while still accepting $30/11$, $56\\%$, and
    $100 \\times 1.2$.
    """
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


def _inside_span(index: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in spans)


def check_currency_symbol(row) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []

    for field in TEXT_FIELDS:
        text = cell_text(row, field)
        if not text:
            continue

        math_spans = _math_dollar_spans(text)
        for match in re.finditer(r"\$\s*\d+(?:[,.]\d+)?", text):
            if _inside_span(match.start(), math_spans):
                continue
            issues.append(
                make_issue(
                    "Major",
                    "Currency Symbol Error",
                    field,
                    match.group(0),
                    '"$" appears to be used as a currency symbol. Use words like rupees/dollars for currency and reserve $...$ for math formatting.',
                    confidence="High",
                )
            )

    return issues
