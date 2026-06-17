import re
from fractions import Fraction
from math import gcd
from typing import Iterable, List, Optional, Sequence, Tuple

from .constants import VALID_KEYS
from .models import ReviewIssue, make_issue
from .normalize import cell_text
from .option_equivalence import match_answer_to_options


ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "thirtieth": 30,
    "fortieth": 40,
    "fiftieth": 50,
    "sixtieth": 60,
}

CARDINAL_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ratio_tuple(text: str) -> Optional[Tuple[int, int]]:
    value = str(text or "").strip().strip("$ ")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", value)
    if not match:
        return None
    a = Fraction(match.group(1))
    b = Fraction(match.group(2))
    den_lcm = a.denominator * b.denominator // gcd(a.denominator, b.denominator)
    ia = int(a * den_lcm)
    ib = int(b * den_lcm)
    if ia == 0 and ib == 0:
        return None
    g = gcd(abs(ia), abs(ib)) or 1
    return ia // g, ib // g


def _ratio_text(ratio: Tuple[int, int]) -> str:
    return f"{ratio[0]}:{ratio[1]}"


def _key_option_text(row) -> str:
    key = cell_text(row, "Key")
    if key in VALID_KEYS:
        return cell_text(row, key)
    return ""


def _clean_num(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    decimal = float(value)
    text = f"{decimal:.10f}".rstrip("0").rstrip(".")
    return text


def _fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _percentage_text(value: Fraction) -> str:
    return f"{_clean_num(value)}%"


def _unique_option_matches(row, answer_candidates: Sequence[str]) -> List[str]:
    matches: List[str] = []
    for candidate in answer_candidates:
        if not candidate:
            continue
        for match in match_answer_to_options(candidate, row):
            if match not in matches:
                matches.append(match)
    return matches


def _key_consequence_issue(
    row,
    answer_candidates: Sequence[str],
    evidence: str,
    reason: str,
    *,
    no_option_reason: str | None = None,
) -> List[ReviewIssue]:
    """Add a Key Mismatch/Answer Not in Options issue for deterministic corrections.

    This is intentionally conservative: it only creates a consequence issue when
    the corrected answer has exactly one matching option, or when no option matches
    and the caller explicitly says that this is a real consequence.
    """
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, answer_candidates)
    if len(matches) == 1:
        corrected_key = matches[0]
        if corrected_key != key:
            return [
                make_issue(
                    "Critical",
                    "Key Mismatch",
                    "Key",
                    f"Corrected answer: {answer_candidates[0]}; Matching option: {corrected_key}; Key: {key}",
                    reason,
                    suggested_fix=corrected_key,
                    confidence="High",
                )
            ]
        return []
    if not matches and no_option_reason:
        return [
            make_issue(
                "Critical",
                "Answer Not in Options",
                "Options",
                evidence,
                no_option_reason,
                suggested_fix=f"Correct answer = {answer_candidates[0]}",
                confidence="High",
            )
        ]
    return []


def _value_from_word_or_number(text: str) -> Optional[int]:
    raw = str(text or "").strip().lower()
    raw = raw.strip(".,;:()")
    digit = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", raw)
    if digit:
        return int(digit.group(1))
    raw = raw.replace("-", " ")
    if raw in ORDINAL_WORDS:
        return ORDINAL_WORDS[raw]
    if raw in CARDINAL_WORDS:
        return CARDINAL_WORDS[raw]
    parts = raw.split()
    if len(parts) == 2 and parts[0] in CARDINAL_WORDS:
        tens = CARDINAL_WORDS[parts[0]]
        unit = ORDINAL_WORDS.get(parts[1]) or CARDINAL_WORDS.get(parts[1])
        if tens >= 20 and unit is not None:
            return tens + unit
    return None


def _answer_candidates_for_fraction(value: Fraction, unit: str = "") -> List[str]:
    candidates = []
    base_fraction = _fraction_text(value)
    base_decimal = _clean_num(value)
    for base in dict.fromkeys([base_fraction, base_decimal]):
        candidates.append(f"{base}{unit}")
        if unit and not unit.startswith(" "):
            candidates.append(f"{base} {unit}")
    if unit:
        candidates.append(base_fraction)
        candidates.append(base_decimal)
    return list(dict.fromkeys(candidates))


# ---------------------------------------------------------------------------
# Alligation / mixture
# ---------------------------------------------------------------------------


def _extract_answer_ratio_from_explanation(explanation: str) -> Optional[Tuple[int, int]]:
    # Prefer ratios near final-answer words, otherwise use the last ratio in the explanation.
    candidates = []
    for match in re.finditer(r"\b(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\b", explanation):
        ratio = _ratio_tuple(match.group(0))
        if ratio is None:
            continue
        context = explanation[max(0, match.start() - 70): match.end() + 70].lower()
        score = 1 if any(word in context for word in ["answer", "therefore", "hence", "required", "option"]) else 0
        candidates.append((score, match.start(), ratio))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def _parse_alligation_values(question: str) -> Optional[Tuple[Fraction, Fraction, Fraction]]:
    lower = question.lower()
    if not any(word in lower for word in ["mixture", "mixed", "mix"]):
        return None
    if not any(word in lower for word in ["worth", "price", "cost", "value"]):
        return None

    # Common form: tea worth 20 ... tea worth 50 ... mixture worth 30.
    money_numbers = [Fraction(n) for n in re.findall(r"(?:worth|price|cost|value)\s*(?:is|of|=)?\s*(\d+(?:\.\d+)?)", lower)]
    if len(money_numbers) >= 3:
        return money_numbers[0], money_numbers[1], money_numbers[-1]

    # Common form: milk worth 40 is mixed with water to make a mixture worth 30.
    first_match = re.search(r"(\w+)\s+worth\s+(\d+(?:\.\d+)?)", lower)
    mean_match = re.search(r"mixture\s+worth\s+(\d+(?:\.\d+)?)", lower)
    if first_match and mean_match and "water" in lower:
        return Fraction(first_match.group(2)), Fraction(0), Fraction(mean_match.group(1))

    return None


def _compute_alligation_ratio(first: Fraction, second: Fraction, mean: Fraction, question: str) -> Optional[Tuple[int, int]]:
    low = min(first, second)
    high = max(first, second)
    if not (low < mean < high):
        return None

    qty_low = high - mean
    qty_high = mean - low
    lower = question.lower()

    if re.search(r"cheaper\s*:?\s*dearer|low(?:er)?\s*:?\s*high(?:er)?", lower):
        a, b = qty_low, qty_high
    elif "milk" in lower and "water" in lower:
        # Water is the lower-value component. Preserve the asked order milk:water.
        if first > second:
            a, b = qty_high, qty_low
        else:
            a, b = qty_low, qty_high
    else:
        # Default to the order in which the two components are mentioned.
        if first <= second:
            a, b = qty_low, qty_high
        else:
            a, b = qty_high, qty_low

    den_lcm = a.denominator * b.denominator // gcd(a.denominator, b.denominator)
    ia = int(a * den_lcm)
    ib = int(b * den_lcm)
    g = gcd(abs(ia), abs(ib)) or 1
    return ia // g, ib // g


def check_alligation_ratio(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    if not question:
        return []

    parsed = _parse_alligation_values(question)
    if not parsed:
        return []
    first, second, mean = parsed
    correct_ratio = _compute_alligation_ratio(first, second, mean, question)
    if not correct_ratio:
        return []

    issues: List[ReviewIssue] = []
    correct_ratio_text = _ratio_text(correct_ratio)
    key_text = _key_option_text(row)
    key_ratio = _ratio_tuple(key_text)
    explanation_ratio = _extract_answer_ratio_from_explanation(explanation)

    if explanation_ratio is not None and explanation_ratio != correct_ratio:
        issues.append(
            make_issue(
                "Critical",
                "Formula Error",
                "Explanation",
                f"Explanation ratio: {_ratio_text(explanation_ratio)}; Correct alligation ratio: {correct_ratio_text}",
                "The alligation ratio is reversed or set up incorrectly. The ratio of quantities should use the cross-differences from the mean.",
                suggested_fix=f"Correct ratio = {correct_ratio_text}",
                confidence="High",
            )
        )

    if key_ratio is not None and key_ratio != correct_ratio:
        matches = match_answer_to_options(correct_ratio_text, row)
        suggested = matches[0] if len(matches) == 1 else correct_ratio_text
        issues.append(
            make_issue(
                "Critical",
                "Key Mismatch",
                "Key",
                f"Key ratio: {_ratio_text(key_ratio)}; Correct alligation ratio: {correct_ratio_text}",
                "The marked key does not match the alligation ratio derived from the given values.",
                suggested_fix=suggested,
                confidence="High",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Ranking / row-position rules
# ---------------------------------------------------------------------------


def check_same_person_opposite_end_total(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    lower = question.lower()
    if not any(term in lower for term in ["from the top", "from the bottom", "from the left", "from the right", "from the front", "from the back"]):
        return []
    if not re.search(r"how\s+many\s+(?:students|boys|girls|persons|people)\s+(?:are\s+there|in|present)", lower):
        return []

    rank_token = r"(?:\d+(?:st|nd|rd|th)?|[a-z]+(?:-[a-z]+)?)"
    rank_prefix = r"(?:ranks?|ranked|position(?:\s+is)?|is|at)?\s*"
    patterns = [
        rf"{rank_prefix}({rank_token})\s+from\s+the\s+top.*?{rank_prefix}({rank_token})\s+from\s+the\s+bottom",
        rf"{rank_prefix}({rank_token})\s+from\s+the\s+bottom.*?{rank_prefix}({rank_token})\s+from\s+the\s+top",
        rf"{rank_prefix}({rank_token})\s+from\s+the\s+left.*?{rank_prefix}({rank_token})\s+from\s+the\s+right",
        rf"{rank_prefix}({rank_token})\s+from\s+the\s+right.*?{rank_prefix}({rank_token})\s+from\s+the\s+left",
        rf"{rank_prefix}({rank_token})\s+from\s+the\s+front.*?{rank_prefix}({rank_token})\s+from\s+the\s+back",
        rf"{rank_prefix}({rank_token})\s+from\s+the\s+back.*?{rank_prefix}({rank_token})\s+from\s+the\s+front",
    ]
    found: Optional[Tuple[int, int]] = None
    for pattern in patterns:
        match = re.search(pattern, lower)
        if not match:
            continue
        first = _value_from_word_or_number(match.group(1))
        second = _value_from_word_or_number(match.group(2))
        if first is not None and second is not None:
            found = (first, second)
            break
    if not found:
        return []

    total = found[0] + found[1] - 1
    total_text = str(total)
    key = cell_text(row, "Key")
    matches = match_answer_to_options(total_text, row)
    if len(matches) == 1 and matches[0] == key and total_text in explanation:
        return []

    issues: List[ReviewIssue] = []
    # If the explanation substitutes different ranks, make the root issue explicit.
    numbers_in_explanation = {int(n) for n in re.findall(r"\b\d+\b", explanation)}
    if found[0] not in numbers_in_explanation or found[1] not in numbers_in_explanation:
        issues.append(
            make_issue(
                "Critical",
                "Wrong Substitution",
                "Explanation",
                f"Question ranks: {found[0]} and {found[1]}; explanation text: {explanation[:160]}",
                "The explanation does not use the rank values given in the question.",
                confidence="High",
            )
        )

    issues.extend(
        _key_consequence_issue(
            row,
            [total_text],
            f"Correct total is {found[0]} + {found[1]} - 1 = {total_text}.",
            "After using the ranks from the question, the marked key/options do not match the corrected total.",
            no_option_reason="After using the given ranks, the corrected total is not present in any option.",
        )
    )
    return issues


def check_students_below_above_count(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    lower = question.lower()
    if not re.search(r"how\s+many\s+students\s+are\s+(?:below|above|behind|ahead of|after|before)", lower):
        return []
    total_match = re.search(r"class\s+of\s+(\d+)\s+students", lower)
    rank_match = re.search(r"(?:is|ranked|ranks?)\s+(\d+)(?:st|nd|rd|th)?\s+from\s+the\s+(top|bottom)", lower)
    if not total_match or not rank_match:
        return []
    total = int(total_match.group(1))
    rank = int(rank_match.group(1))
    side = rank_match.group(2)

    asks_below = any(term in lower for term in ["below", "behind", "after"])
    asks_above = any(term in lower for term in ["above", "ahead of", "before"])
    if side == "top" and asks_below:
        correct = total - rank
    elif side == "top" and asks_above:
        correct = rank - 1
    elif side == "bottom" and asks_above:
        correct = total - rank
    elif side == "bottom" and asks_below:
        correct = rank - 1
    else:
        return []

    correct_text = str(correct)
    key = cell_text(row, "Key")
    matches = match_answer_to_options(correct_text, row)
    if len(matches) == 1 and matches[0] == key and correct_text in explanation:
        return []

    evidence = f"Question total: {total}; rank: {rank} from {side}; correct count: {correct_text}."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "For a count of students below/above a ranked person, exclude the person themselves. This is a count, not a position from the opposite end.",
            suggested_fix=f"Correct count = {correct_text}",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, [correct_text], evidence, "The corrected ranking count maps to a different option/key."))
    return issues


def check_left_right_count_mismatch(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    lower_q = question.lower()
    lower_e = explanation.lower()
    if not re.search(r"how\s+many\s+students\s+are\s+to\s+(?:his|her|their|\w+'s)?\s*right", lower_q):
        return []
    total_match = re.search(r"row\s+of\s+(\d+)\s+students", lower_q)
    pos_match = re.search(r"(?:is|ranked|ranks?)\s+(\d+)(?:st|nd|rd|th)?\s+from\s+the\s+left", lower_q)
    if not total_match or not pos_match:
        return []
    total = int(total_match.group(1))
    position = int(pos_match.group(1))
    correct = total - position
    correct_text = str(correct)
    if "left" not in lower_e:
        return []

    key = cell_text(row, "Key")
    matches = match_answer_to_options(correct_text, row)
    if len(matches) == 1 and matches[0] == key and correct_text in explanation:
        return []

    evidence = f"Question asks students to the right; explanation calculates students to the left. Correct right-side count is {total} - {position} = {correct_text}."
    issues = [
        make_issue(
            "Critical",
            "Question-Explanation Mismatch",
            "Explanation",
            evidence,
            "The explanation solves the opposite side from what the question asks.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, [correct_text], evidence, "The corrected right-side count maps to a different option/key."))
    return issues


def check_minor_label_mismatch(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    lower = question.lower()
    if not re.search(r"\brow\s+of\s+(boys|girls)\b", lower):
        return []
    if not re.search(r"how\s+many\s+students\s+are\s+there\s+in\s+the\s+class", lower):
        return []

    # Keep this minor rule only for otherwise consistent same-person opposite-end totals.
    rank_token = r"(?:\d+(?:st|nd|rd|th)?|[a-z]+(?:-[a-z]+)?)"
    rank_prefix = r"(?:ranks?|ranked|position(?:\s+is)?|is|at)?\s*"
    match = re.search(rf"{rank_prefix}({rank_token})\s+from\s+the\s+top.*?{rank_prefix}({rank_token})\s+from\s+the\s+bottom", lower)
    if match:
        first = _value_from_word_or_number(match.group(1))
        second = _value_from_word_or_number(match.group(2))
        if first is not None and second is not None:
            total_text = str(first + second - 1)
            key = cell_text(row, "Key")
            matches = match_answer_to_options(total_text, row)
            if not (len(matches) == 1 and matches[0] == key):
                return []

    return [
        make_issue(
            "Minor",
            "Question Wording / Label Mismatch",
            "Question",
            question,
            "The context says a row of boys/girls, but the question asks for students in the class. The wording should use a consistent label.",
            suggested_fix="Use a consistent label, e.g., 'How many boys are there in the row?' or adjust the context to a class of students.",
            confidence="High",
        )
    ]


# ---------------------------------------------------------------------------
# Arithmetic-topic consequence checks
# ---------------------------------------------------------------------------


def check_profit_percentage_base(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "profit percentage" not in lower and "profit %" not in lower:
        return []
    match = re.search(r"(?:bought|costs?|cost price|cp|item costs?|article is bought)\s+(?:for\s+)?(\d+(?:\.\d+)?).*?(?:sold|selling price|sp)\s+(?:for\s+)?(\d+(?:\.\d+)?)", lower)
    if not match:
        return []
    cp = Fraction(match.group(1))
    sp = Fraction(match.group(2))
    if cp == 0:
        return []
    pct = (sp - cp) * 100 / cp
    candidates = [_percentage_text(pct)]
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, candidates)
    if len(matches) == 1 and matches[0] == key:
        return []
    evidence = f"CP = {_clean_num(cp)}, SP = {_clean_num(sp)}, so profit percentage = ({_clean_num(sp)} - {_clean_num(cp)}) / {_clean_num(cp)} × 100 = {candidates[0]}."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "Profit percentage must be calculated on cost price, not selling price or profit amount alone.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected profit percentage maps to a different option/key."))
    return issues


def check_percentage_increase_base(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "percentage increase" not in lower:
        return []
    match = re.search(r"increases\s+from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)", lower)
    if not match:
        return []
    old = Fraction(match.group(1))
    new = Fraction(match.group(2))
    if old == 0:
        return []
    pct = (new - old) * 100 / old
    candidates = [_percentage_text(pct)]
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, candidates)
    if len(matches) == 1 and matches[0] == key:
        return []
    evidence = f"Original price = {_clean_num(old)}, new price = {_clean_num(new)}, so percentage increase = ({_clean_num(new)} - {_clean_num(old)}) / {_clean_num(old)} × 100 = {candidates[0]}."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "Percentage increase must use the original value as the base.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected percentage increase maps to a different option/key."))
    return issues


def check_average_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    issues: List[ReviewIssue] = []

    sum_match = re.search(r"average\s+of\s+(\d+)\s+numbers\s+is\s+(\d+(?:\.\d+)?).*?find\s+the\s+sum", lower)
    if sum_match:
        count = Fraction(sum_match.group(1))
        avg = Fraction(sum_match.group(2))
        total = count * avg
        candidates = [_clean_num(total)]
        key = cell_text(row, "Key")
        matches = _unique_option_matches(row, candidates)
        if not (len(matches) == 1 and matches[0] == key):
            evidence = f"Sum = average × count = {_clean_num(avg)} × {_clean_num(count)} = {candidates[0]}."
            issues.append(
                make_issue(
                    "Critical",
                    "Formula Error",
                    "Explanation",
                    evidence,
                    "The sum of values is average multiplied by the number of values, not average divided by count.",
                    confidence="High",
                )
            )
            issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected sum maps to a different option/key."))
        return issues

    removed_match = re.search(
        r"average\s+of\s+(\d+)\s+numbers\s+is\s+(\d+(?:\.\d+)?).*?remaining\s+(\d+)\s+numbers\s+becomes\s+(\d+(?:\.\d+)?).*?removed",
        lower,
    )
    if removed_match:
        count = Fraction(removed_match.group(1))
        avg = Fraction(removed_match.group(2))
        remaining_count = Fraction(removed_match.group(3))
        remaining_avg = Fraction(removed_match.group(4))
        removed = count * avg - remaining_count * remaining_avg
        candidates = [_clean_num(removed)]
        key = cell_text(row, "Key")
        matches = _unique_option_matches(row, candidates)
        if not (len(matches) == 1 and matches[0] == key):
            evidence = f"Removed number = ({_clean_num(count)} × {_clean_num(avg)}) - ({_clean_num(remaining_count)} × {_clean_num(remaining_avg)}) = {candidates[0]}."
            issues.append(
                make_issue(
                    "Critical",
                    "Formula Error",
                    "Explanation",
                    evidence,
                    "The removed number should be found from the difference of total sums, not the difference of averages.",
                    confidence="High",
                )
            )
            issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected removed number maps to a different option/key."))
    return issues


def check_speed_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "speed" not in lower:
        return []
    match = re.search(r"covers?\s+(\d+(?:\.\d+)?)\s*km\s+in\s+(\d+(?:\.\d+)?)\s+hours?", lower)
    if not match:
        return []
    distance = Fraction(match.group(1))
    time = Fraction(match.group(2))
    if time == 0:
        return []
    speed = distance / time
    candidates = [f"{_clean_num(speed)} km/h", f"{_clean_num(speed)} kmph", _clean_num(speed)]
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, candidates)
    if len(matches) == 1 and matches[0] == key:
        return []
    evidence = f"Speed = distance / time = {_clean_num(distance)} / {_clean_num(time)} = {_clean_num(speed)} km/h."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "Speed should be distance divided by time, not distance multiplied by time.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected speed maps to a different option/key."))
    return issues


def check_simple_interest_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "simple interest" not in lower:
        return []
    match = re.search(r"on\s+(\d+(?:\.\d+)?)\s+at\s+(\d+(?:\.\d+)?)\s*%\s+per\s+annum\s+for\s+(\d+(?:\.\d+)?)\s+years?", lower)
    if not match:
        return []
    principal = Fraction(match.group(1))
    rate = Fraction(match.group(2))
    time = Fraction(match.group(3))
    si = principal * rate * time / 100
    candidates = [_clean_num(si)]
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, candidates)
    if len(matches) == 1 and matches[0] == key:
        return []
    # If the correct SI doesn't appear in any option at all, the issue is with the
    # options/key (handled by final_answer_checks), not the formula itself.
    if not matches:
        return []
    evidence = f"Simple interest = P × R × T / 100 = {_clean_num(principal)} × {_clean_num(rate)} × {_clean_num(time)} / 100 = {candidates[0]}."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "Simple interest must be divided by 100, not by 10 or any other base.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected simple interest maps to a different option/key."))
    return issues


def check_clock_angle_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "clock" not in lower and "hands" not in lower:
        return []
    if "angle" not in lower:
        return []

    time_match = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", lower)
    if time_match:
        hour = int(time_match.group(1)) % 12
        minute = int(time_match.group(2))
    else:
        oclock_match = re.search(r"\b(\d{1,2})\s*o'?clock\b", lower)
        if not oclock_match:
            return []
        hour = int(oclock_match.group(1)) % 12
        minute = 0

    angle = abs(Fraction(30 * hour, 1) - Fraction(11 * minute, 2))
    if angle > 180:
        angle = 360 - angle
    angle_text = _clean_num(angle)
    candidates = [f"{angle_text} degrees", f"{angle_text}°", angle_text]
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, candidates)
    if len(matches) == 1 and matches[0] == key:
        return []

    evidence = f"Clock angle at {hour or 12}:{minute:02d} is |30h - 5.5m| = {angle_text} degrees using the smaller-angle convention."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "The explanation uses an incorrect clock-angle formula or misses the smaller-angle convention.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected clock angle maps to a different option/key."))
    return issues


def check_time_work_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "complete" not in lower or "work" not in lower:
        return []
    match = re.search(r"a\s+can\s+complete.*?in\s+(\d+(?:\.\d+)?)\s+days?.*?b\s+can\s+complete.*?in\s+(\d+(?:\.\d+)?)\s+days?", lower)
    if not match:
        return []
    a_days = Fraction(match.group(1))
    b_days = Fraction(match.group(2))
    combined_rate = Fraction(1, a_days) + Fraction(1, b_days)
    if combined_rate == 0:
        return []

    if "fraction" in lower:
        days_match = re.search(r"in\s+(\d+(?:\.\d+)?)\s+days?", lower[match.end():]) or re.search(r"together\s+in\s+(\d+(?:\.\d+)?)\s+days?", lower)
        if not days_match:
            return []
        days = Fraction(days_match.group(1))
        done = days * combined_rate
        candidates = _answer_candidates_for_fraction(done)
        evidence = f"Together work in {_clean_num(days)} days = {_clean_num(days)} × (1/{_clean_num(a_days)} + 1/{_clean_num(b_days)}) = {_fraction_text(done)}."
    else:
        total_days = Fraction(1, combined_rate)
        candidates = _answer_candidates_for_fraction(total_days, " days")
        evidence = f"Together time = 1 / (1/{_clean_num(a_days)} + 1/{_clean_num(b_days)}) = {_clean_num(total_days)} days."

    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, candidates)
    if len(matches) == 1 and matches[0] == key:
        return []
    # If computed together-time doesn't appear in any option, the question is likely
    # asking for partial work or remaining work — not the simple together-time.
    if not matches:
        return []

    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "For time-work questions, combine work rates, not the raw number of days.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected time-work result maps to a different option/key."))
    return issues


def check_divisibility_by_9(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    match = re.search(r"\bis\s+(\d+)\s+divisible\s+by\s+9\b", lower)
    if not match:
        return []
    number_text = match.group(1)
    digit_sum = sum(int(ch) for ch in number_text)
    answer = "Yes" if digit_sum % 9 == 0 else "No"
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, [answer])
    if len(matches) == 1 and matches[0] == key:
        return []
    evidence = f"Digit sum of {number_text} is {digit_sum}; {digit_sum} is {'divisible' if digit_sum % 9 == 0 else 'not divisible'} by 9. Correct answer: {answer}."
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "The divisibility conclusion does not follow from the divisibility rule for 9.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, [answer], evidence, "The corrected divisibility conclusion maps to a different option/key."))
    return issues


# ---------------------------------------------------------------------------
# Direction / ambiguity / wording checks from V2.2
# ---------------------------------------------------------------------------


def _turn(facing: str, turn_direction: str) -> str:
    order = ["north", "east", "south", "west"]
    idx = order.index(facing)
    if turn_direction == "right":
        return order[(idx + 1) % 4]
    return order[(idx - 1) % 4]


def _move(x: int, y: int, facing: str, distance: int) -> Tuple[int, int]:
    if facing == "north":
        return x, y + distance
    if facing == "south":
        return x, y - distance
    if facing == "east":
        return x + distance, y
    return x - distance, y


def _format_displacement(x: int, y: int) -> Optional[str]:
    if x == 0 and y == 0:
        return "Starting point"
    if x != 0 and y == 0:
        return f"{abs(x)} m {'East' if x > 0 else 'West'}"
    if y != 0 and x == 0:
        return f"{abs(y)} m {'North' if y > 0 else 'South'}"
    ns = f"{abs(y)} m {'North' if y > 0 else 'South'}"
    ew = f"{abs(x)} m {'East' if x > 0 else 'West'}"
    return f"{ns}, {ew}"


def check_direction_path_trace(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if not any(direction in lower for direction in ["north", "south", "east", "west"]):
        return []
    if "turn" not in lower or "walk" not in lower:
        return []

    first = re.search(r"walks?\s+(\d+)\s*m\s+(north|south|east|west)", lower)
    if not first:
        return []
    x = y = 0
    facing = first.group(2)
    x, y = _move(x, y, facing, int(first.group(1)))

    remainder = lower[first.end():]
    for turn_direction, distance in re.findall(r"turns?\s+(left|right)\s+and\s+walks?\s+(\d+)\s*m", remainder):
        facing = _turn(facing, turn_direction)
        x, y = _move(x, y, facing, int(distance))

    final_text = _format_displacement(x, y)
    if not final_text:
        return []
    key = cell_text(row, "Key")
    matches = _unique_option_matches(row, [final_text])
    if len(matches) == 1 and matches[0] == key:
        return []

    evidence = f"Coordinate trace ends at ({x}, {y}), so the displacement is {final_text}."
    issues = [
        make_issue(
            "Critical",
            "Final Conclusion Mismatch",
            "Explanation",
            evidence,
            "A coordinate trace of the stated movements gives a different final direction from the explanation/key.",
            suggested_fix=final_text,
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, [final_text], evidence, "The corrected direction maps to a different option/key."))
    return issues


def check_age_time_direction(row) -> List[ReviewIssue]:
    """Catch simple age questions where the wording says past but the equation has a future solution, or vice versa."""
    question = cell_text(row, "Question")
    lower = question.lower()
    if "years" not in lower or "old" not in lower or "times" not in lower:
        return []
    if not any(phrase in lower for phrase in ["years ago", "year ago", "after", "years hence", "from now"]):
        return []

    ages = [int(n) for n in re.findall(r"\b(\d{1,3})\s+years?\s+old\b", lower)]
    multiplier_match = re.search(r"\b(\d+(?:\.\d+)?)\s+times\b", lower)
    if len(ages) < 2 or not multiplier_match:
        return []

    younger, older = sorted(ages[:2])
    multiplier = Fraction(multiplier_match.group(1))
    if multiplier == 1:
        return []

    # Positive x means future: older + x = m(younger + x).
    future_x = Fraction(older - multiplier * younger, multiplier - 1)
    asks_past = "ago" in lower
    asks_future = any(phrase in lower for phrase in ["after", "hence", "from now"])

    if asks_past and future_x > 0:
        return [
            make_issue(
                "Critical",
                "Clarity Issue",
                "Question",
                f"Question asks 'years ago', but the valid time is {future_x} years in the future.",
                "The age relation is not satisfied in the past; the wording should ask for a future time or the options/question should be corrected.",
                confidence="High",
            )
        ]
    if asks_future and future_x < 0:
        return [
            make_issue(
                "Critical",
                "Clarity Issue",
                "Question",
                f"Question asks for a future time, but the valid time is {-future_x} years ago.",
                "The age relation is not satisfied in the future; the wording should ask for a past time or the options/question should be corrected.",
                confidence="High",
            )
        ]
    return []


def check_ambiguous_difference_wording(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if re.search(r"difference\s+between\s+\d+(?:\.\d+)?\s+and\s+\d+(?:\.\d+)?\s+times\s+\d+(?:\.\d+)?", lower):
        return [
            make_issue(
                "Major",
                "Clarity Issue",
                "Question",
                question,
                "The wording allows more than one mathematical interpretation. Specify whether to multiply before or after taking the difference.",
                confidence="High",
            )
        ]
    return []


def check_pronoun_reference_ambiguity(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    if re.search(r"\b[A-Z][a-z]+\s+told\s+[A-Z][a-z]+\s+that\s+(his|her)\b", question):
        names = re.findall(r"\b[A-Z][a-z]+\b", question)
        if len(names) >= 2:
            return [
                make_issue(
                    "Major",
                    "Clarity Issue",
                    "Question",
                    question,
                    "The pronoun reference is unclear because it can refer to more than one person mentioned before it.",
                    confidence="High",
                )
            ]
    return []


def check_two_item_total_cost(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    lower = question.lower()
    if "total cost" not in lower or "each" not in lower or " and " not in lower:
        return []

    match = re.search(
        r"(\d+)\s+\w+s?\s+at\s+(\d+(?:\.\d+)?)\s+each\s+and\s+(\d+)\s+\w+s?\s+at\s+(\d+(?:\.\d+)?)\s+each",
        lower,
    )
    if not match:
        return []
    n1, p1, n2, p2 = Fraction(match.group(1)), Fraction(match.group(2)), Fraction(match.group(3)), Fraction(match.group(4))
    total = n1 * p1 + n2 * p2
    total_text = str(total.numerator) if total.denominator == 1 else f"{total.numerator}/{total.denominator}"

    key_matches = match_answer_to_options(total_text, row)
    key = cell_text(row, "Key")
    if key_matches and key in key_matches and total_text in explanation:
        return []

    if key_matches and key not in key_matches:
        return [
            make_issue(
                "Critical",
                "Final Conclusion Mismatch",
                "Explanation",
                f"Question requires total cost {total_text}; explanation/key does not use that total.",
                "The explanation appears to calculate only part of the total cost instead of adding all listed items.",
                suggested_fix=key_matches[0] if len(key_matches) == 1 else total_text,
                confidence="High",
            )
        ]
    if total_text not in explanation:
        return [
            make_issue(
                "Critical",
                "Final Conclusion Mismatch",
                "Explanation",
                f"Question requires total cost {total_text}; explanation gives a partial total.",
                "The explanation appears to calculate only part of the total cost instead of adding all listed items.",
                confidence="High",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Compound interest
# ---------------------------------------------------------------------------


def check_compound_interest_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "compound interest" not in lower and " c.i." not in lower and "(c.i.)" not in lower:
        return []
    # Difference between CI and SI uses a different formula — skip
    if "difference" in lower:
        return []
    # Half-yearly / quarterly compounding requires rate/period adjustment — skip
    if any(term in lower for term in ["half-yearly", "half yearly", "quarterly", "monthly"]):
        return []

    # Extract P at R% for N years from standard patterns
    match = re.search(
        r"(?:rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)"     # principal
        r"[^.]*?"
        r"at\s+(\d+(?:\.\d+)?)\s*%"               # rate
        r"[^.]*?"
        r"for\s+(\d+)\s+years?",                   # time
        lower,
    )
    if not match:
        return []

    principal = Fraction(match.group(1).replace(",", ""))
    rate = Fraction(match.group(2))
    n = int(match.group(3))
    if n > 4 or principal <= 0:
        return []

    base = (Fraction(100) + rate) / Fraction(100)
    amount = principal * (base ** n)
    ci = amount - principal

    asks_amount = any(p in lower for p in ["find the amount", "total amount", "final amount", "amount after", "amount at the end"])
    target = amount if asks_amount else ci
    label = "Amount" if asks_amount else "CI"
    candidates = [_clean_num(target)]

    key = cell_text(row, "Key")
    opt_matches = _unique_option_matches(row, candidates)
    if len(opt_matches) == 1 and opt_matches[0] == key:
        return []
    if not opt_matches:
        return []

    if asks_amount:
        formula_str = (f"A = P(1 + R/100)^N = {_clean_num(principal)} × "
                       f"(1 + {_clean_num(rate)}/100)^{n} = {candidates[0]}.")
    else:
        formula_str = (f"CI = P(1 + R/100)^N - P = {_clean_num(principal)} × "
                       f"(1 + {_clean_num(rate)}/100)^{n} − {_clean_num(principal)} = {candidates[0]}.")
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            formula_str,
            f"Compound interest formula: CI = P(1+R/100)^N − P. The correct {label} is {candidates[0]}.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, formula_str, f"The corrected {label} maps to a different option/key."))
    return issues


# ---------------------------------------------------------------------------
# Pipes and cisterns
# ---------------------------------------------------------------------------


def check_pipes_cisterns_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "pipe" not in lower and "tap" not in lower:
        return []
    if "fill" not in lower:
        return []
    if "together" not in lower and "both" not in lower and "simultaneously" not in lower:
        return []
    # One-filling + one-emptying requires sign reversal — skip for now
    if "empty" in lower or "drain" in lower or "leak" in lower:
        return []

    fill_times = re.findall(
        r"(?:pipe|tap)\s+[a-z]\s+(?:can\s+)?fill[^.]*?(?:tank|cistern|pool)"
        r"[^.]*?in\s+(\d+(?:\.\d+)?)\s+hours?",
        lower,
    )
    if len(fill_times) < 2:
        return []

    a_h = Fraction(fill_times[0])
    b_h = Fraction(fill_times[1])
    if a_h <= 0 or b_h <= 0:
        return []

    combined_rate = Fraction(1) / a_h + Fraction(1) / b_h
    total_h = Fraction(1) / combined_rate
    candidates = _answer_candidates_for_fraction(total_h, " hours")

    key = cell_text(row, "Key")
    opt_matches = _unique_option_matches(row, candidates)
    if len(opt_matches) == 1 and opt_matches[0] == key:
        return []
    if not opt_matches:
        return []

    evidence = (f"Together fill time = 1 / (1/{_clean_num(a_h)} + 1/{_clean_num(b_h)}) "
                f"= {_fraction_text(total_h)} hours.")
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "For pipe/cistern problems, combine fill rates (1/time each) then take the reciprocal for total time.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected fill time maps to a different option/key."))
    return issues


# ---------------------------------------------------------------------------
# Discount → Selling Price
# ---------------------------------------------------------------------------


def check_discount_formula(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "discount" not in lower:
        return []
    if not any(p in lower for p in ["selling price", "s.p.", " sp ", "find sp", "find the sp"]):
        return []

    mp_match = re.search(
        r"(?:marked\s+price|m\.?p\.?|list\s+price)\s*(?:is|=|:)?\s*(?:rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)",
        lower,
    )
    d_match = re.search(r"discount\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*%", lower)
    if not mp_match or not d_match:
        return []

    mp = Fraction(mp_match.group(1).replace(",", ""))
    d = Fraction(d_match.group(1))
    if mp <= 0 or d <= 0 or d >= 100:
        return []

    sp = mp * (Fraction(100) - d) / Fraction(100)
    candidates = [_clean_num(sp)]

    key = cell_text(row, "Key")
    opt_matches = _unique_option_matches(row, candidates)
    if len(opt_matches) == 1 and opt_matches[0] == key:
        return []
    if not opt_matches:
        return []

    evidence = (f"SP = MP × (100 − discount%) / 100 = "
                f"{_clean_num(mp)} × (100 − {_clean_num(d)}) / 100 = {candidates[0]}.")
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "Selling price = Marked price × (100 − discount%) / 100.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected selling price maps to a different option/key."))
    return issues


# ---------------------------------------------------------------------------
# Average speed — round trip / equal-distance legs
# ---------------------------------------------------------------------------


def check_average_speed_round_trip(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    lower = question.lower()
    if "average speed" not in lower:
        return []
    # Must indicate an equal-distance return journey
    if not any(term in lower for term in ["returns", "return", "comes back", "back to"]):
        return []

    # Exactly two speed values are expected; more legs → different formula
    speeds = re.findall(r"(\d+(?:\.\d+)?)\s*km/?h(?:our)?s?\b", lower)
    if len(speeds) != 2:
        speeds = re.findall(r"(\d+(?:\.\d+)?)\s*kmph\b", lower)
    if len(speeds) != 2:
        return []

    s1 = Fraction(speeds[0])
    s2 = Fraction(speeds[1])
    if s1 <= 0 or s2 <= 0:
        return []

    avg = 2 * s1 * s2 / (s1 + s2)
    candidates = [f"{_clean_num(avg)} km/h", f"{_clean_num(avg)} kmph", _clean_num(avg)]

    key = cell_text(row, "Key")
    opt_matches = _unique_option_matches(row, candidates)
    if len(opt_matches) == 1 and opt_matches[0] == key:
        return []
    if not opt_matches:
        return []

    evidence = (f"Average speed = 2ab/(a+b) = "
                f"2×{_clean_num(s1)}×{_clean_num(s2)} / ({_clean_num(s1)}+{_clean_num(s2)}) "
                f"= {_clean_num(avg)} km/h.")
    issues = [
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            evidence,
            "For equal-distance round trips, average speed = 2ab/(a+b), not the arithmetic mean (a+b)/2.",
            confidence="High",
        )
    ]
    issues.extend(_key_consequence_issue(row, candidates, evidence, "The corrected average speed maps to a different option/key."))
    return issues


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


_HCF_PATTERN = re.compile(
    r"\bHCF\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*=\s*(\d+)", re.IGNORECASE
)
_LCM_PATTERN = re.compile(
    r"\bLCM\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*=\s*(\d+)", re.IGNORECASE
)


def check_hcf_lcm_result(row) -> List[ReviewIssue]:
    """Flag when HCF(a,b)=x or LCM(a,b)=x states the wrong value."""
    text = cell_text(row, "Explanation")
    if not text:
        return []
    issues: List[ReviewIssue] = []
    for m in _HCF_PATTERN.finditer(text):
        a, b, stated = int(m.group(1)), int(m.group(2)), int(m.group(3))
        correct = gcd(a, b)
        if correct != stated:
            issues.append(
                make_issue(
                    "Critical",
                    "Calculation Error",
                    "Explanation",
                    m.group(0),
                    f"HCF({a}, {b}) = {correct}, not {stated}.",
                    suggested_fix=f"HCF({a}, {b}) = {correct}",
                    confidence="High",
                )
            )
    for m in _LCM_PATTERN.finditer(text):
        a, b, stated = int(m.group(1)), int(m.group(2)), int(m.group(3))
        correct = (a * b) // gcd(a, b)
        if correct != stated:
            issues.append(
                make_issue(
                    "Critical",
                    "Calculation Error",
                    "Explanation",
                    m.group(0),
                    f"LCM({a}, {b}) = {correct}, not {stated}.",
                    suggested_fix=f"LCM({a}, {b}) = {correct}",
                    confidence="High",
                )
            )
    return issues


_PROB_PATTERN = re.compile(
    r"\bP\s*\([^)]{0,50}\)\s*=\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE
)


def check_probability_value(row) -> List[ReviewIssue]:
    """Flag when an explicit P(E) = k/n has k > n (probability > 1)."""
    text = cell_text(row, "Explanation")
    if not text:
        return []
    issues: List[ReviewIssue] = []
    for m in _PROB_PATTERN.finditer(text):
        num, den = int(m.group(1)), int(m.group(2))
        if den == 0 or num <= den:
            continue
        issues.append(
            make_issue(
                "Critical",
                "Calculation Error",
                "Explanation",
                m.group(0),
                f"Probability {num}/{den} is greater than 1, which is impossible. "
                f"The number of favourable outcomes cannot exceed the total outcomes.",
                confidence="High",
            )
        )
    return issues


def check_topic_specific(row) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []

    # High-confidence deterministic topic validators and consequence checks.
    issues.extend(check_alligation_ratio(row))
    issues.extend(check_same_person_opposite_end_total(row))
    issues.extend(check_students_below_above_count(row))
    issues.extend(check_left_right_count_mismatch(row))
    issues.extend(check_profit_percentage_base(row))
    issues.extend(check_percentage_increase_base(row))
    issues.extend(check_average_formula(row))
    issues.extend(check_speed_formula(row))
    issues.extend(check_simple_interest_formula(row))
    issues.extend(check_clock_angle_formula(row))
    issues.extend(check_time_work_formula(row))
    issues.extend(check_divisibility_by_9(row))
    issues.extend(check_direction_path_trace(row))

    # New formula checks.
    issues.extend(check_compound_interest_formula(row))
    issues.extend(check_pipes_cisterns_formula(row))
    issues.extend(check_discount_formula(row))
    issues.extend(check_average_speed_round_trip(row))
    issues.extend(check_hcf_lcm_result(row))
    issues.extend(check_probability_value(row))

    # Clarity / wording checks.
    issues.extend(check_age_time_direction(row))
    issues.extend(check_ambiguous_difference_wording(row))
    issues.extend(check_pronoun_reference_ambiguity(row))
    issues.extend(check_two_item_total_cost(row))
    issues.extend(check_minor_label_mismatch(row))
    return issues
