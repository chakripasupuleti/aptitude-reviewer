import ast
import operator as op
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import List, Optional

from .constants import VALID_KEYS
from .models import ReviewIssue, make_issue
from .normalize import cell_text
from .option_equivalence import UNIT_TO_BASE, match_answer_to_options, parse_quantity
from .python_sandbox import safe_eval_expr

_ALLOWED_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}

RESULT_VALUE_PATTERN = (
    r"-?\d+\s+\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?"  # mixed fraction: 2 1/2
    r"|-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?"     # simple fraction: 3/4
    r"|-?\d+(?:\.\d+)?%"                              # percentage: 25%
    r"|-?\d+(?:\.\d+)?"                               # integer/decimal
)


def _decimal_fraction(text: str) -> Optional[Fraction]:
    try:
        return Fraction(Decimal(text.replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def _safe_eval(node) -> Fraction:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Fraction(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPERATORS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        return Fraction(_ALLOWED_OPERATORS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPERATORS:
        operand = _safe_eval(node.operand)
        return Fraction(_ALLOWED_OPERATORS[type(node.op)](operand))
    raise ValueError("Unsupported arithmetic expression")


def _replace_mixed_numbers(expr: str) -> str:
    """Convert mixed numbers like 1 1/2 into (1+1/2) before whitespace removal."""

    def repl(match: re.Match) -> str:
        whole = int(match.group(1))
        numerator = match.group(2)
        denominator = match.group(3)
        if whole < 0:
            return f"({whole}-{numerator}/{denominator})"
        return f"({whole}+{numerator}/{denominator})"

    # Avoid matching digits that are part of decimals. This is intentionally conservative.
    return re.sub(r"(?<![\d.])(-?\d+)\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", repl, expr)


def evaluate_expression(expr: str) -> Optional[Fraction]:
    clean = expr.replace(",", "")
    clean = clean.replace("×", "*").replace("÷", "/")
    clean = clean.replace("\\times", "*").replace("\\div", "/")
    clean = clean.replace("−", "-")
    clean = _replace_mixed_numbers(clean)
    clean = re.sub(r"\s+", "", clean)
    if not re.fullmatch(r"[0-9+\-*/().]+", clean):
        return None
    try:
        tree = ast.parse(clean, mode="eval")
        return _safe_eval(tree)
    except Exception:
        # Fall back to subprocess sandbox for expressions the AST walker can't handle
        return safe_eval_expr(clean)


def parse_result_value(text: str) -> Optional[Fraction]:
    value = text.strip().replace(",", "")

    mixed = re.fullmatch(r"(-?\d+)\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", value)
    if mixed:
        whole = Fraction(int(mixed.group(1)), 1)
        num = _decimal_fraction(mixed.group(2))
        den = _decimal_fraction(mixed.group(3))
        if num is not None and den not in (None, Fraction(0)):
            frac = num / den
            return whole - frac if whole < 0 else whole + frac

    frac = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", value)
    if frac:
        num = _decimal_fraction(frac.group(1))
        den = _decimal_fraction(frac.group(2))
        if num is not None and den not in (None, Fraction(0)):
            return num / den

    pct = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*%", value)
    if pct:
        num = _decimal_fraction(pct.group(1))
        if num is not None:
            return num / 100

    num = re.fullmatch(r"-?\d+(?:\.\d+)?", value)
    if num:
        return _decimal_fraction(value)

    return None


def parse_stated_value_for_expression(stated_text: str, expression: str) -> Optional[Fraction]:
    value = stated_text.strip()
    pct = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*%", value)
    if pct:
        pct_as_number = _decimal_fraction(pct.group(1))
        expr_clean = expression.replace(" ", "").replace("×", "*").replace("\\times", "*")
        # In expressions like 0.25 * 100 = 25%, the RHS percentage represents
        # the displayed percent number (25), not the fraction value (0.25).
        if re.search(r"\*100(?:\.0+)?\b", expr_clean):
            return pct_as_number
        # Also accept when expression result = percentage number directly
        # e.g. "100/8 = 12.5%" means "rate is 12.5 percent", not "= 0.125"
        computed = evaluate_expression(expression.replace(",", ""))
        if computed is not None and pct_as_number is not None and _close_enough(computed, pct_as_number):
            return pct_as_number
    return parse_result_value(stated_text)


def _format_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    whole = abs(value.numerator) // value.denominator
    remainder = abs(value.numerator) % value.denominator
    sign = "-" if value < 0 else ""
    if whole and remainder:
        return f"{sign}{whole} {remainder}/{value.denominator}"
    return f"{value.numerator}/{value.denominator}"


def _close_enough(left: Fraction, right: Fraction) -> bool:
    return abs(float(left - right)) < 1e-9


def _close_enough_for_display(actual: Fraction, stated: Fraction, stated_text: str) -> bool:
    """Accept ordinary rounding in displayed decimal answers.

    Example: 100/600*100 = 16.67 is acceptable because the exact value is
    16.666..., rounded to two decimals. Integer answers remain strict.
    Also accepts tiny relative differences (< 0.01%) that arise from
    floating-point accumulation when inputs have many decimal places.
    """
    if _close_enough(actual, stated):
        return True
    match = re.search(r"-?\d+\.(\d+)", str(stated_text or ""))
    if match:
        decimals = len(match.group(1))
        tolerance = Fraction(1, 2 * (10 ** decimals))
        if abs(actual - stated) <= tolerance:
            return True
    # Accept floating-point noise: relative difference < 0.01% of the magnitude.
    # E.g. 785886.85 × 1.16985856 = 919376.42 (stated) vs 919376.458 (computed)
    # has relative error ~4×10⁻⁸ — well below this threshold.
    abs_diff = abs(float(actual - stated))
    magnitude = max(abs(float(actual)), 1.0)
    if abs_diff / magnitude < 1e-4:
        return True
    return False


def _next_nonspace(text: str, index: int) -> str:
    while index < len(text) and text[index].isspace():
        index += 1
    return text[index] if index < len(text) else ""


def _add_corrected_answer_key_issue(issues: List[ReviewIssue], row, corrected_text: str, evidence: str) -> None:
    """Add a key issue when a corrected deterministic answer contradicts the marked key.

    If the corrected answer maps to exactly one option, Reviewed Key can be filled.
    If it maps to multiple options and the marked key is not among them, we still flag
    the key as wrong but avoid guessing a single corrected key.
    """
    key = cell_text(row, "Key")
    if key not in VALID_KEYS:
        return

    matches = match_answer_to_options(corrected_text, row)
    if len(matches) == 1 and matches[0] != key:
        issues.append(
            make_issue(
                "Critical",
                "Key Mismatch",
                "Key",
                f"Corrected value: {corrected_text}; Matching option: {matches[0]}; Key: {key}",
                f"After correcting the calculation, the answer is {corrected_text}, which matches {matches[0]}, but the key is marked as {key}.",
                suggested_fix=matches[0],
                confidence="High",
            )
        )
    elif len(matches) > 1 and key not in matches:
        issues.append(
            make_issue(
                "Critical",
                "Key Mismatch",
                "Key",
                f"Corrected value: {corrected_text}; Matching options: {', '.join(matches)}; Key: {key}",
                f"After correcting the calculation, the answer is {corrected_text}, which matches {', '.join(matches)}, so the key cannot be {key}.",
                confidence="High",
            )
        )


def _add_corrected_key_issue(issues: List[ReviewIssue], row, corrected_value: Fraction, evidence: str) -> None:
    """If a corrected deterministic value maps to options, add key mismatch if needed."""
    _add_corrected_answer_key_issue(issues, row, _format_fraction(corrected_value), evidence)


def _format_quantity_amount(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    # Prefer short decimal for common terminating fractions used in unit conversions.
    decimal_value = float(value)
    if abs(decimal_value - round(decimal_value, 4)) < 1e-10:
        return (f"{decimal_value:.4f}".rstrip("0").rstrip("."))
    return _format_fraction(value)


def _unit_factor(unit: str):
    normalized = unit.lower().strip().rstrip(".")
    return UNIT_TO_BASE.get(normalized) or UNIT_TO_BASE.get(normalized + "s")


def _check_quantity_equalities(row, text: str) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []
    number_token = r"(?:-?\d+\s+\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?|-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
    quantity_pattern = re.compile(
        rf"({number_token}\s+[A-Za-z]+s?)\s*=\s*({number_token}\s+[A-Za-z]+s?)",
        flags=re.IGNORECASE,
    )

    for match in quantity_pattern.finditer(text):
        # Skip compound quantities like "3 hours 30 minutes = 3.5 hours" where the
        # matched left side ("30 minutes") is actually part of a larger expression.
        if re.search(r"[A-Za-z]\s*$", text[: match.start()]):
            continue
        left_text = match.group(1)
        right_text = match.group(2)
        left = parse_quantity(left_text)
        right = parse_quantity(right_text)
        if left is None or right is None:
            continue
        if left.value[0] != right.value[0]:
            continue
        if left.value[1] == right.value[1]:
            continue

        right_unit_match = re.search(r"([A-Za-z]+s?)\s*$", right_text)
        suggested = ""
        corrected_answer_text = ""
        if right_unit_match:
            right_unit = right_unit_match.group(1)
            unit_info = _unit_factor(right_unit)
            if unit_info:
                _, right_factor = unit_info
                corrected_amount = left.value[1] / right_factor
                corrected_amount_text = _format_quantity_amount(corrected_amount)
                corrected_answer_text = f"{corrected_amount_text} {right_unit}"
                suggested = f"{left_text} = {corrected_answer_text}"

        issues.append(
            make_issue(
                "Critical",
                "Unit Conversion Error",
                "Explanation",
                match.group(0),
                f"The unit conversion is incorrect. {left_text} is not equal to {right_text}.",
                suggested_fix=suggested,
                confidence="High",
            )
        )
        if corrected_answer_text:
            _add_corrected_answer_key_issue(issues, row, corrected_answer_text, match.group(0))

    return issues



def _span_overlaps(span: tuple[int, int], blocked_spans: List[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < blocked_end and end > blocked_start for blocked_start, blocked_end in blocked_spans)


def _format_percent_number(value: Fraction) -> str:
    """Format a displayed percent number, not its fractional equivalent."""
    return _format_quantity_amount(value)


def _check_profit_percentage_flow(text: str) -> tuple[List[ReviewIssue], List[tuple[int, int]]]:
    """Detect misleading profit-percentage flow after an incorrect profit value.

    Example:
    Profit = 75 - 60 = 10. Profit percentage = 10/60 * 100 = 25%.
    The second equality is mathematically false if read literally, but the better
    correction is to carry the corrected profit forward:
    Profit = 75 - 60 = 15; Profit percentage = 15/60 * 100 = 25%.
    """
    issues: List[ReviewIssue] = []
    blocked_spans: List[tuple[int, int]] = []

    profit_pattern = re.compile(
        rf"\bProfit\s*=\s*({RESULT_VALUE_PATTERN})\s*-\s*({RESULT_VALUE_PATTERN})\s*=\s*({RESULT_VALUE_PATTERN})",
        flags=re.IGNORECASE,
    )
    percent_pattern = re.compile(
        rf"\bProfit\s*(?:percentage|%)\s*=\s*({RESULT_VALUE_PATTERN})\s*/\s*({RESULT_VALUE_PATTERN})\s*\*\s*100\s*=\s*({RESULT_VALUE_PATTERN})",
        flags=re.IGNORECASE,
    )

    for profit_match in profit_pattern.finditer(text):
        minuend = parse_result_value(profit_match.group(1))
        subtrahend = parse_result_value(profit_match.group(2))
        stated_profit = parse_result_value(profit_match.group(3))
        if minuend is None or subtrahend is None or stated_profit is None:
            continue
        actual_profit = minuend - subtrahend
        if _close_enough(actual_profit, stated_profit):
            continue

        # Look for a nearby profit-percentage expression after the incorrect profit line.
        search_window = text[profit_match.end(): profit_match.end() + 180]
        percent_match = percent_pattern.search(search_window)
        if not percent_match:
            continue

        absolute_span = (profit_match.end() + percent_match.start(), profit_match.end() + percent_match.end())
        numerator = parse_result_value(percent_match.group(1))
        denominator = parse_result_value(percent_match.group(2))
        displayed_percent = parse_stated_value_for_expression(percent_match.group(3), "*100")
        if numerator is None or denominator in (None, Fraction(0)) or displayed_percent is None:
            continue

        if not _close_enough(numerator, stated_profit):
            continue

        corrected_percent_number = actual_profit / denominator * 100
        corrected_percent_text = _format_percent_number(corrected_percent_number)
        suggested = (
            f"Profit = {profit_match.group(1)} - {profit_match.group(2)} = {_format_fraction(actual_profit)}; "
            f"Profit percentage = {_format_fraction(actual_profit)}/{percent_match.group(2)} * 100 = {corrected_percent_text}%"
        )
        issues.append(
            make_issue(
                "Critical",
                "Wrong Substitution",
                "Explanation",
                search_window[percent_match.start():percent_match.end()],
                f"The profit percentage step uses {percent_match.group(1)} as the profit, but the corrected profit is {_format_fraction(actual_profit)}.",
                suggested_fix=suggested,
                confidence="High",
            )
        )
        blocked_spans.append(absolute_span)

    return issues, blocked_spans


def _strip_latex(text: str) -> str:
    """Convert common LaTeX math constructs to plain arithmetic so the checker can verify them.

    Examples:
      $\\frac{540}{11}$  →  540/11
      $\\frac{a+b}{c}$   →  (a+b)/c   (parens added for nested expressions)
      $30^\\circ$        →  30
      $\\text{...}$      →  (removed)
    """
    # \frac{numerator}{denominator}
    result = re.sub(
        r"\\frac\{([^{}]+)\}\{([^{}]+)\}",
        lambda m: f"({m.group(1)})/({m.group(2)})",
        text,
    )
    # ^{exponent} or ^\circ — strip entirely (we don't check exponents)
    result = re.sub(r"\^(?:\{[^{}]*\}|\\[a-z]+|\w)", "", result)
    # \text{...} — replace with placeholder so adjacent operators don't merge into **
    result = re.sub(r"\\text\{[^{}]*\}", "X", result)
    # Surrounding $ ... $ delimiters
    result = re.sub(r"\$", " ", result)
    # Preserve arithmetic operators before generic command stripping
    result = result.replace("\\times", "*").replace("\\div", "/")
    result = result.replace("\\cdot", "*")
    # Remaining lone backslash-commands like \approx, \rightarrow
    result = re.sub(r"\\[a-zA-Z]+", " ", result)
    return result


def check_arithmetic(row) -> List[ReviewIssue]:
    text = cell_text(row, "Explanation")
    issues: List[ReviewIssue] = []
    if not text:
        return issues

    normalized = _strip_latex(text)
    normalized = normalized.replace("×", "*").replace("÷", "/")
    normalized = normalized.replace("\\times", "*").replace("\\div", "/")
    # Normalize Unicode minus variants to ASCII hyphen so equality patterns match correctly
    normalized = normalized.replace("−", "-").replace("–", "-").replace("—", "-")
    # Indian number format: 2,00,000 → 200000; 1,26,000 → 126000 (lakhs/crores use
    # groups of 2 digits after the first group, then 3 at the end). This must run
    # BEFORE the Western thousands-separator pass below: that pattern's word-boundary
    # match can partially match inside an Indian-grouped number (e.g. it grabs the
    # "00,000" tail of "1,00,000"), leaving a stray comma that then no longer fits
    # the Indian shape and silently truncates the parsed value (1,00,000 -> "1").
    normalized = re.sub(r"\b(\d{1,2}(?:,\d{2})+,\d{3})\b", lambda m: m.group(0).replace(",", ""), normalized)
    # Remove thousands separators from numbers (1,650 → 1650, 7,500 → 7500) so the
    # equality pattern doesn't truncate or backtrack at commas.
    normalized = re.sub(r"\b(\d{1,3}(?:,\d{3})+)\b", lambda m: m.group(0).replace(",", ""), normalized)

    issues.extend(_check_quantity_equalities(row, normalized))
    flow_issues, blocked_spans = _check_profit_percentage_flow(normalized)
    issues.extend(flow_issues)

    # Percentage expressions like 25% of 200 = 60.
    # Skip intermediate conversion chains such as: 25% of 200 = 25/100 * 200 = 50.
    percent_pattern = re.compile(
        rf"(\d+(?:\.\d+)?)\s*%\s+of\s+(\d+(?:\.\d+)?)\s*=\s*({RESULT_VALUE_PATTERN})",
        flags=re.IGNORECASE,
    )
    for match in percent_pattern.finditer(normalized):
        if _next_nonspace(normalized, match.end()) in {"/", "*", "×", "÷"}:
            continue
        # Skip when this percentage is the right-hand side of a larger expression,
        # e.g. "100 + 40% of 100 = 140" — the 140 is not just "40% of 100".
        preceding = normalized[:match.start()].rstrip()
        if preceding and preceding[-1] in {"+", "-", "*", "/"}:
            continue
        pct = _decimal_fraction(match.group(1))
        base = _decimal_fraction(match.group(2))
        stated = parse_result_value(match.group(3))
        if pct is None or base is None or stated is None:
            continue
        actual = pct / 100 * base
        if not _close_enough_for_display(actual, stated, match.group(3)):
            issues.append(
                make_issue(
                    "Critical",
                    "Percentage Calculation Error",
                    "Explanation",
                    match.group(0),
                    f"The percentage calculation is incorrect. Correct value is {_format_fraction(actual)}.",
                    suggested_fix=f"{match.group(1)}% of {match.group(2)} = {_format_fraction(actual)}",
                )
            )
            _add_corrected_key_issue(issues, row, actual, match.group(0))

    # Simple arithmetic equality expressions like 12 + 43 = 54, (540)/(11) = 39,
    # 1 1/2 + 1/2 = 2, or 75/100 = 3/4.
    # Leading ( allowed so that LaTeX-stripped fractions like (540)/(11) are caught.
    equality_pattern = re.compile(
        rf"(?<![\w])([(\d][0-9\s,\.()+\-*/]*[+\-*/][0-9\s,\.()+\-*/]*|[0-9]+\s+[0-9]+\s*/\s*[0-9]+)\s*=\s*({RESULT_VALUE_PATTERN})(?!\s*/)",
        flags=re.IGNORECASE,
    )
    # Manual search loop instead of finditer so that when a chained/intermediate
    # step is skipped (result is followed by an operator), we restart from the
    # result position. This allows catching the *next* step in the chain, e.g.:
    #   12 + 44 - 1 = 12 + 43 = 54
    # The first match (12+44-1=12) is skipped (12 is followed by +), but we
    # restart at "12" so the next match (12+43=54) is checked.
    seen_spans: set = set()
    pos = 0
    while pos < len(normalized):
        match = equality_pattern.search(normalized, pos)
        if not match:
            break
        span = match.span()
        if span in seen_spans or _span_overlaps(span, blocked_spans):
            pos = match.end()
            continue
        seen_spans.add(span)
        if _next_nonspace(normalized, match.end()) in {"+", "-", "*", "/", "=", "×", "÷", ":"}:
            # Chained intermediate step or ratio component (e.g. "(30-0) = 15 : 30") —
            # don't report, restart from result position so subsequent step is evaluated.
            pos = match.start(2)
            continue
        # Skip integer division with remainder notation: "45/4 = 11 remainder 1" or "8/5 = 1 r 3"
        # Also covers "= 36 with a remainder of 3" phrasing.
        if re.match(r"\s+(?:(?:with\s+a?\s+)?remainder(?:\s+of)?\s*|r\s+)\d+\b", normalized[match.end():], re.IGNORECASE):
            pos = match.end()
            continue
        # Skip sub-expressions that are operands in a larger equation (e.g. x * (1 - 0.15) = 595
        # — the (1 - 0.15) sub-expression is preceded by * so it's not a standalone equality).
        preceding = normalized[:match.start()].rstrip()
        if preceding and preceding[-1] in {'+', '-', '*', '/'}:
            pos = match.end()
            continue
        expression = match.group(1).strip()
        # Skip expressions that span multiple lines — these are accidental multi-line matches
        # caused by newlines inside the pattern's \s character class (e.g. "4\n147/4 = 36").
        if '\n' in expression:
            pos = match.end()
            continue
        stated_text = match.group(2).strip()
        actual = evaluate_expression(expression)
        stated = parse_stated_value_for_expression(stated_text, expression)
        if actual is None or stated is None:
            pos = match.end()
            continue
        if not _close_enough_for_display(actual, stated, stated_text):
            issues.append(
                make_issue(
                    "Critical",
                    "Calculation Error",
                    "Explanation",
                    match.group(0),
                    f"The calculation is incorrect. Correct value is {_format_fraction(actual)}.",
                    suggested_fix=f"{expression} = {_format_fraction(actual)}",
                )
            )
            _add_corrected_key_issue(issues, row, actual, match.group(0))
        pos = match.end()

    return issues
