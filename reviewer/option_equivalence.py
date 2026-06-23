import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import List, Optional, Tuple

from .constants import OPTION_COLUMNS
from .models import ReviewIssue, make_issue
from .normalize import cell_text


@dataclass(frozen=True)
class ParsedValue:
    kind: str
    value: object
    display: str


def clean_text(text: str) -> str:
    value = str(text or "").strip()
    value = value.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    value = re.sub(r"^\$|\$$", "", value.strip())
    # Normalize degree symbol to word so "105°" parses the same as "105 degrees"
    value = re.sub(r"°", " degrees", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_exact(text: str) -> str:
    value = clean_text(text).lower()
    value = re.sub(r"\s+", "", value)
    value = value.strip(". ;,")
    return value


def _decimal_to_fraction(text: str) -> Optional[Fraction]:
    try:
        return Fraction(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _parse_simple_number(text: str) -> Optional[Fraction]:
    text = clean_text(text)
    text = text.replace(",", "")

    # LaTeX fraction: \frac{a}{b}
    latex_match = re.fullmatch(r"\\frac\s*\{\s*(-?\d+(?:\.\d+)?)\s*\}\s*\{\s*(-?\d+(?:\.\d+)?)\s*\}", text)
    if latex_match:
        num = _decimal_to_fraction(latex_match.group(1))
        den = _decimal_to_fraction(latex_match.group(2))
        if num is not None and den not in (None, Fraction(0)):
            return num / den

    # Mixed LaTeX fraction: 1\frac{1}{2}
    mixed_latex = re.fullmatch(r"(-?\d+)\s*\\frac\s*\{\s*(\d+(?:\.\d+)?)\s*\}\s*\{\s*(\d+(?:\.\d+)?)\s*\}", text)
    if mixed_latex:
        whole = Fraction(int(mixed_latex.group(1)), 1)
        num = _decimal_to_fraction(mixed_latex.group(2))
        den = _decimal_to_fraction(mixed_latex.group(3))
        if num is not None and den not in (None, Fraction(0)):
            frac = num / den
            return whole - frac if whole < 0 else whole + frac

    # Mixed normal fraction: 1 1/2
    mixed = re.fullmatch(r"(-?\d+)\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if mixed:
        whole = Fraction(int(mixed.group(1)), 1)
        num = _decimal_to_fraction(mixed.group(2))
        den = _decimal_to_fraction(mixed.group(3))
        if num is not None and den not in (None, Fraction(0)):
            frac = num / den
            return whole - frac if whole < 0 else whole + frac

    # Simple fraction: 1/2
    frac = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", text)
    if frac:
        num = _decimal_to_fraction(frac.group(1))
        den = _decimal_to_fraction(frac.group(2))
        if num is not None and den not in (None, Fraction(0)):
            return num / den

    # Percentage: 50%
    pct = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*%", text)
    if pct:
        num = _decimal_to_fraction(pct.group(1))
        if num is not None:
            return num / 100

    # Plain number
    num = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    if num:
        return _decimal_to_fraction(text)

    return None


def is_clock_time(text: str) -> bool:
    value = clean_text(text).lower()
    return bool(re.search(r"\b\d{1,2}\s*:\s*\d{2}\s*(am|pm)\b", value))


def parse_ratio(text: str) -> Optional[ParsedValue]:
    value = clean_text(text)
    if is_clock_time(value):
        return None
    if not re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*:\s*-?\d+(?:\.\d+)?\s*", value):
        return None
    left, right = [part.strip() for part in value.split(":", 1)]
    a = _parse_simple_number(left)
    b = _parse_simple_number(right)
    if a is None or b is None:
        return None
    if a == 0:
        normalized = (a, b)
    else:
        normalized = (Fraction(1), b / a)
    return ParsedValue("ratio", normalized, value)


CURRENCY_ALIASES = {
    "$": "dollar",
    "usd": "dollar",
    "dollar": "dollar",
    "dollars": "dollar",
    "€": "euro",
    "eur": "euro",
    "euro": "euro",
    "euros": "euro",
    "₹": "rupee",
    "rs": "rupee",
    "rs.": "rupee",
    "rupee": "rupee",
    "rupees": "rupee",
    "£": "pound",
    "gbp": "pound",
    "pound": "pound",
    "pounds": "pound",
}


def parse_currency(text: str) -> Optional[ParsedValue]:
    raw = str(text or "").strip()
    raw = raw.strip(".;,")
    raw = re.sub(r"\s+", " ", raw)
    # Strip thousands-separator commas: ₹12,000 → ₹12000
    raw = re.sub(r"(?<=\d),(?=\d{3}\b)", "", raw)

    # Symbol before amount: $13, € 10, ₹100. Do not treat $7/2 as currency.
    symbol_first = re.fullmatch(r"([$€₹£])\s*(-?\d+(?:\.\d+)?)", raw)
    if symbol_first:
        code = CURRENCY_ALIASES.get(symbol_first.group(1))
        amount = _decimal_to_fraction(symbol_first.group(2))
        if code and amount is not None:
            return ParsedValue("currency", (code, amount), raw)

    # Amount before symbol: 13€, 100₹.
    symbol_last = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([$€₹£])", raw)
    if symbol_last:
        code = CURRENCY_ALIASES.get(symbol_last.group(2))
        amount = _decimal_to_fraction(symbol_last.group(1))
        if code and amount is not None:
            return ParsedValue("currency", (code, amount), raw)

    # Amount with currency word: 13 dollars, 100 rupees, 10 euros, Rs. 50.
    word_last = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s+([A-Za-z.]+)", raw, flags=re.IGNORECASE)
    if word_last:
        code = CURRENCY_ALIASES.get(word_last.group(2).lower())
        amount = _decimal_to_fraction(word_last.group(1))
        if code and amount is not None:
            return ParsedValue("currency", (code, amount), raw)

    word_first = re.fullmatch(r"([A-Za-z.]+)\s*(-?\d+(?:\.\d+)?)", raw, flags=re.IGNORECASE)
    if word_first:
        code = CURRENCY_ALIASES.get(word_first.group(1).lower())
        amount = _decimal_to_fraction(word_first.group(2))
        if code and amount is not None:
            return ParsedValue("currency", (code, amount), raw)

    return None


UNIT_TO_BASE = {
    # time -> seconds
    "second": ("time", Fraction(1)),
    "seconds": ("time", Fraction(1)),
    "sec": ("time", Fraction(1)),
    "secs": ("time", Fraction(1)),
    "s": ("time", Fraction(1)),
    "minute": ("time", Fraction(60)),
    "minutes": ("time", Fraction(60)),
    "min": ("time", Fraction(60)),
    "mins": ("time", Fraction(60)),
    "hour": ("time", Fraction(3600)),
    "hours": ("time", Fraction(3600)),
    "hr": ("time", Fraction(3600)),
    "hrs": ("time", Fraction(3600)),
    # length -> metres
    "mm": ("length", Fraction(1, 1000)),
    "millimetre": ("length", Fraction(1, 1000)),
    "millimeter": ("length", Fraction(1, 1000)),
    "cm": ("length", Fraction(1, 100)),
    "centimetre": ("length", Fraction(1, 100)),
    "centimeter": ("length", Fraction(1, 100)),
    "m": ("length", Fraction(1)),
    "metre": ("length", Fraction(1)),
    "meter": ("length", Fraction(1)),
    "km": ("length", Fraction(1000)),
    "kilometre": ("length", Fraction(1000)),
    "kilometer": ("length", Fraction(1000)),
    # weight -> grams
    "mg": ("weight", Fraction(1, 1000)),
    "g": ("weight", Fraction(1)),
    "gram": ("weight", Fraction(1)),
    "grams": ("weight", Fraction(1)),
    "kg": ("weight", Fraction(1000)),
    "kilogram": ("weight", Fraction(1000)),
    "kilograms": ("weight", Fraction(1000)),
    # volume -> litres
    "ml": ("volume", Fraction(1, 1000)),
    "millilitre": ("volume", Fraction(1, 1000)),
    "milliliter": ("volume", Fraction(1, 1000)),
    "l": ("volume", Fraction(1)),
    "litre": ("volume", Fraction(1)),
    "liter": ("volume", Fraction(1)),
    "litres": ("volume", Fraction(1)),
    "liters": ("volume", Fraction(1)),
    # angle -> degrees
    "degree": ("angle", Fraction(1)),
    "degrees": ("angle", Fraction(1)),
    "deg": ("angle", Fraction(1)),
}


def parse_quantity(text: str) -> Optional[ParsedValue]:
    value = clean_text(text).lower()
    # Keep this conservative: number followed immediately by a known unit.
    match = re.fullmatch(r"(.+?)\s+([a-z]+)s?", value)
    if not match:
        return None
    number_text, unit = match.group(1).strip(), match.group(2).strip()
    unit_info = UNIT_TO_BASE.get(unit) or UNIT_TO_BASE.get(unit + "s")
    if not unit_info:
        return None
    number = _parse_simple_number(number_text)
    if number is None:
        return None
    category, factor = unit_info
    return ParsedValue("quantity", (category, number * factor), value)


def parse_numeric(text: str) -> Optional[ParsedValue]:
    value = clean_text(text)
    number = _parse_simple_number(value)
    if number is None:
        return None
    return ParsedValue("number", number, value)


def parse_option_value(text: str) -> Optional[ParsedValue]:
    for parser in (parse_ratio, parse_currency, parse_quantity, parse_numeric):
        parsed = parser(text)
        if parsed is not None:
            return parsed
    # Fallback: "NUMBER unknown_word(s)" — strip trailing non-unit words and parse as number.
    # Handles options like "1,380 packages" or "2 batches" where the unit isn't in UNIT_TO_BASE.
    stripped = re.sub(r"\s+[A-Za-z][A-Za-z\s]*$", "", clean_text(text)).strip()
    if stripped and stripped != clean_text(text).strip():
        number = _parse_simple_number(stripped)
        if number is not None:
            return ParsedValue("number", number, text)
    return None


def values_equivalent(left: str, right: str) -> bool:
    if normalize_exact(left) == normalize_exact(right):
        return True
    left_parsed = parse_option_value(left)
    right_parsed = parse_option_value(right)
    if left_parsed is None or right_parsed is None:
        return False
    if left_parsed.kind == right_parsed.kind and left_parsed.value == right_parsed.value:
        return True
    # Cross-type: plain number matches currency of same amount (e.g. "12000" == "₹12,000")
    if {left_parsed.kind, right_parsed.kind} == {"number", "currency"}:
        num_p = left_parsed if left_parsed.kind == "number" else right_parsed
        cur_p = left_parsed if left_parsed.kind == "currency" else right_parsed
        return num_p.value == cur_p.value[1]
    return False


def match_answer_to_options(answer_text: str, row) -> List[str]:
    matches: List[str] = []
    answer = clean_text(answer_text).strip(". ;,")
    if not answer:
        return matches

    for option_col in OPTION_COLUMNS:
        option_text = cell_text(row, option_col)
        if not option_text:
            continue
        if values_equivalent(answer, option_text):
            matches.append(option_col)
    return matches


def check_duplicate_options(row) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []
    options: List[Tuple[str, str]] = [(col, cell_text(row, col)) for col in OPTION_COLUMNS if cell_text(row, col)]

    for i in range(len(options)):
        col_i, text_i = options[i]
        for j in range(i + 1, len(options)):
            col_j, text_j = options[j]
            if normalize_exact(text_i) == normalize_exact(text_j):
                issues.append(
                    make_issue(
                        "Critical",
                        "Duplicate Options",
                        f"{col_i}, {col_j}",
                        f"{col_i}: {text_i}; {col_j}: {text_j}",
                        f"{col_i} and {col_j} are identical.",
                    )
                )
                continue

            parsed_i = parse_option_value(text_i)
            parsed_j = parse_option_value(text_j)
            if parsed_i is not None and parsed_j is not None and parsed_i.kind == parsed_j.kind and parsed_i.value == parsed_j.value:
                # For ratios, different display text = simplified vs unsimplified = valid MCQ distractor
                if parsed_i.kind == "ratio" and normalize_exact(text_i) != normalize_exact(text_j):
                    continue
                issues.append(
                    make_issue(
                        "Critical",
                        "Duplicate Options",
                        f"{col_i}, {col_j}",
                        f"{col_i}: {text_i}; {col_j}: {text_j}",
                        f"{col_i} and {col_j} are equivalent values.",
                    )
                )

    return issues
