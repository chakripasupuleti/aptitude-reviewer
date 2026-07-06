import re
from typing import Iterable, List, Optional

from .constants import VALID_KEYS
from .models import ReviewIssue, make_issue
from .normalize import cell_text
from .option_equivalence import match_answer_to_options


OPTION_PATTERN = re.compile(r"\bOption\s+[ABCD]\b")
ARITHMETIC_ERROR_TYPES = {"Calculation Error", "Percentage Calculation Error", "Unit Conversion Error"}


def _strip_simple_latex(text: str) -> str:
    """Remove LaTeX delimiters and simple commands so numeric values are visible."""
    # Handle all fraction variants: \frac, \tfrac, \dfrac
    result = re.sub(r"\\[td]?frac\{([^{}]+)\}\{([^{}]+)\}", r"\1/\2", text)
    result = re.sub(r"\^(?:\{[^{}]*\}|\\?[a-zA-Z]+|\w)", "", result)
    result = re.sub(r"\\[a-zA-Z]+\{[^{}]*\}", " ", result)
    # Preserve \% as % so percentage values like 81\% become 81% (matchable by _TERMINAL_VALUE_RE)
    result = result.replace("\\%", "%")
    result = re.sub(r"\\[a-zA-Z]+", " ", result)
    # Strip LaTeX spacing commands: \ (control space), \, \! etc. so they don't block EOL matches
    result = re.sub(r"\\[ ,!;]", " ", result)
    result = re.sub(r"\$", " ", result)
    return result


# Optional unit words that may follow a numeric result (not captured).
_UNIT_SUFFIX = (
    r"(?:\s*(?:st|nd|rd|th"  # ordinal suffixes: 1st, 2nd, 3rd, 4th, 19th, etc.
    r"|degrees?|°|deg|seconds?|sec|minutes?|mins?|hours?|hrs?|"
    r"days?|weeks?|months?|years?|km|m\b|cm|mm|kg|g\b|l\b|liters?|litres?|"
    r"rupees?|rs\.?|usd|inr|units?|items?|students?|persons?|people|boys?|girls?))?"
)

# Matches a simple terminal result value: integer, decimal, fraction, time, or percentage.
_TERMINAL_VALUE_RE = re.compile(
    r"=\s*("
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?"  # time: 5:11 AM
    r"|-?\d+\s+\d+/\d+"                              # mixed fraction: 2 1/11
    r"|-?\d+/\d+"                                    # fraction: 3/4
    r"|-?\d+(?:\.\d+)?\s*%"                          # percentage: 25%
    r"|-?\d+(?:\.\d+)?"                              # integer/decimal
    r")" + _UNIT_SUFFIX + r"\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def extract_final_option(explanation: str) -> Optional[str]:
    if not explanation:
        return None
    candidates = []
    for match in OPTION_PATTERN.finditer(explanation):
        window_start = max(0, match.start() - 50)
        context = explanation[window_start : match.end()].lower()
        if not any(term in context for term in ["answer", "correct", "hence", "therefore", "so", "required"]):
            continue
        # Skip if a sentence boundary separates the qualifying term from this Option
        # mention.  Two forms:
        #   1. ". Option X" — Option starts immediately after a period (between ends with [.!?])
        #   2. ". Capital" inside between — a new sentence begins before reaching Option
        between = explanation[window_start : match.start()]
        if re.search(r"[.!?]\s*$", between) or re.search(r"[.!?]\s+[A-Z]", between):
            continue
        candidates.append(match.group(0))
    return candidates[-1] if candidates else None


def _clean_answer(answer: str) -> str:
    answer = re.sub(r"\bOption\s+[ABCD]\b", "", answer, flags=re.IGNORECASE).strip()
    answer = answer.strip(" :-,")
    # Remove only sentence-ending punctuation, not decimal points.
    answer = re.sub(r"(?<!\d)[.]$", "", answer).strip()
    return answer


def extract_final_answer_text(explanation: str) -> Optional[str]:
    if not explanation:
        return None

    # Stop at a sentence-ending period, but do not stop inside decimals like 3.5.
    answer_capture = r"([^\n;]+?)(?=\.(?:\s|$)|\n|;|$)"
    patterns = [
        rf"(?:therefore|hence|so),?\s*(?:the\s+)?(?:answer|required answer|correct answer)\s*(?:is|=|:)\s*{answer_capture}",
        rf"(?:answer|correct answer|required answer)\s*(?:is|=|:)\s*{answer_capture}",
        rf"(?:therefore|hence|so),?\s*(?:the\s+)?answer\s+{answer_capture}",
        rf"\banswer\s+(?!(?:is\b|=|:)){answer_capture}",
        # "Hence, the true time ... is 5:11 AM." — last sentence conclusion with any noun phrase
        rf"(?:therefore|hence|so),?[^.\n]{{0,120}}\bis\s+{answer_capture}",
    ]

    candidates: List[tuple[int, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, explanation, flags=re.IGNORECASE):
            answer = _clean_answer(match.group(1))
            if 0 < len(answer) <= 80:
                candidates.append((match.start(), answer))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def extract_last_result_value(explanation: str) -> Optional[str]:
    """Return the last bare = VALUE at the end of a line when no explicit answer marker exists.

    Covers explanations that just end with a calculation result such as:
      Number of boys behind Aarav = (50 - 33) = 17
      Total no. of students = 20 + 17 - 1 = 36
    LaTeX is stripped first so lines like '$= 10.5\\ \\text{seconds}$' are handled.
    """
    if not explanation:
        return None
    text = _strip_simple_latex(explanation)
    # Strip trailing whitespace and sentence-ending periods from each line so that
    # "x = 1000." or "= 105 degrees." are handled correctly.
    # A trailing "." is never a decimal point (decimal points are mid-number).
    def _clean(line: str) -> str:
        return line.rstrip().rstrip(".")
    text = "\n".join(_clean(line) for line in text.splitlines())
    matches = list(_TERMINAL_VALUE_RE.finditer(text))
    if not matches:
        return None
    # Multi-step guard: more than one terminal value means the explanation has multiple
    # calculation lines. The last = VALUE is very likely an intermediate step, not the
    # final answer. Require an explicit "hence/answer is" phrase instead.
    if len(matches) > 1:
        return None
    last_match = matches[-1]
    num_part = last_match.group(1).strip()
    # Skip single-digit values — too noisy (likely an intermediate step).
    if re.fullmatch(r"\d", num_part):
        return None
    # Return the full value including any unit suffix (e.g. "10 seconds", "105 degrees")
    # so that match_answer_to_options can compare units correctly.
    full_val = re.sub(r"^\s*=\s*", "", last_match.group(0)).strip()
    return full_val if full_val else num_part


def _parse_numeric(text: str) -> Optional[float]:
    """Parse a simple numeric value (integer, decimal, fraction, mixed fraction)."""
    t = text.strip()
    mixed = re.fullmatch(r"(-?\d+)\s+(\d+)/(\d+)", t)
    if mixed:
        return int(mixed.group(1)) + int(mixed.group(2)) / int(mixed.group(3))
    frac = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", t)
    if frac and float(frac.group(2)) != 0:
        return float(frac.group(1)) / float(frac.group(2))
    num = re.fullmatch(r"-?\d+(?:\.\d+)?", t)
    if num:
        return float(t)
    return None


def _check_conclusion_vs_computation(explanation: str) -> List[ReviewIssue]:
    """Flag when a 'Hence/Therefore' conclusion states a different value than the last computed result.

    Example: computation gives 10.5 seconds but conclusion says 'it will take 11 seconds'.
    """
    # Extract the conclusion sentence (last "Hence/Therefore/So, ..." line)
    conclusion_match = re.search(
        r"(?:hence|therefore|so),?\s+[^\n.]{5,150}[.!]?\s*$",
        explanation,
        re.IGNORECASE | re.MULTILINE,
    )
    if not conclusion_match:
        return []

    conclusion_text = conclusion_match.group(0)
    conclusion_start = conclusion_match.start()

    # Strip LaTeX from conclusion text so mixed fractions like $\tfrac{1}{11}$ become 1/11.
    conclusion_text_clean = _strip_simple_latex(conclusion_text)

    # Extract the main number from the conclusion — use the LAST match to avoid picking up
    # incidental numbers like the time "6" in "when the time is 6:30 is 15 degrees".
    # Capture mixed fractions (49 1/11), plain fractions (11/2), decimals, or integers.
    conc_val_matches = list(re.finditer(
        r"\b(?:take|takes?|require[sd]?|equals?|is|are|be|result[s]?)\s+(-?\d+(?:\s+\d+/\d+|\.\d+|/\d+)?)",
        conclusion_text_clean,
        re.IGNORECASE,
    ))
    if not conc_val_matches:
        return []
    conc_val_str = conc_val_matches[-1].group(1).strip()

    # Find the last = VALUE in the computation body (before the conclusion)
    pre_text = _strip_simple_latex(explanation[:conclusion_start])
    pre_text = "\n".join(line.rstrip() for line in pre_text.splitlines())
    comp_matches = list(_TERMINAL_VALUE_RE.finditer(pre_text))
    if not comp_matches:
        return []
    # Filter: only consider lines that have arithmetic operators before the = sign.
    # This excludes label lines like "Cycle length = 4" or "Exponent = 147"
    # which have no computation — they're just variable assignments.
    def _line_prefix(text: str, match_start: int) -> str:
        ls = text.rfind('\n', 0, match_start)
        return text[ls + 1 if ls >= 0 else 0 : match_start]
    comp_matches = [m for m in comp_matches if re.search(r"[+\-*/×÷]", _line_prefix(pre_text, m.start()))]
    if not comp_matches:
        return []
    comp_val_str = comp_matches[-1].group(1).strip()

    conc_val = _parse_numeric(conc_val_str)
    comp_val = _parse_numeric(comp_val_str)
    if conc_val is None or comp_val is None:
        return []
    if abs(conc_val - comp_val) < 0.01:
        return []

    # The conclusion may state a quantity DERIVED from the last two computed
    # values rather than literally restating the last one — e.g. "A's share = 40",
    # "B's share = 60", "Hence, the difference between their shares is 20". Only
    # flag when the conclusion matches neither the last value nor a simple
    # sum/difference of the last two computed values.
    if len(comp_matches) >= 2:
        prev_val = _parse_numeric(comp_matches[-2].group(1).strip())
        if prev_val is not None and (
            abs(conc_val - abs(comp_val - prev_val)) < 0.01
            or abs(conc_val - (comp_val + prev_val)) < 0.01
        ):
            return []

    return [
        make_issue(
            "Critical",
            "Final Conclusion Mismatch",
            "Explanation",
            f"Computed result: {comp_val_str}; Conclusion states: {conc_val_str}",
            f"The explanation's computation gives {comp_val_str} but the conclusion states {conc_val_str}. The conclusion value does not match the calculated result.",
            confidence="High",
        )
    ]


_COMPUTED_VAL_RE = re.compile(r"=\s*(-?\d{2,}(?:\.\d+)?)", re.IGNORECASE)
_STANDALONE_VAL_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _check_rounding_mismatch(row, explanation: str) -> List[ReviewIssue]:
    """Flag when an explanation rounds a computed value and the rounded result
    still does not match any option — i.e. rounding didn't actually fix anything.

    Only fires when the explanation contains an explicit 'Rounding to/off' phrase.
    Ordinary legitimate rounding (e.g. compound interest = 16.67, rounded to the
    nearest rupee = 17, where 17 is an option) must NOT be flagged — that is
    standard practice, not an error. This only flags the case where neither the
    raw computed value NOR its stated rounded result appears in any option,
    meaning the explanation's answer is disconnected from the options entirely.
    """
    rounding_match = re.search(r"[Rr]ounding\s+(?:to|off)\b", explanation)
    if not rounding_match:
        return []

    pre_rounding = explanation[: rounding_match.start()]
    text = _strip_simple_latex(pre_rounding)
    pre_matches = list(_COMPUTED_VAL_RE.finditer(text))
    if not pre_matches:
        return []

    computed_val_str = pre_matches[-1].group(1).strip()

    if match_answer_to_options(computed_val_str, row):
        return []

    # Legitimate rounding case: the stated rounded result (in the text after the
    # rounding phrase) matches an option. Only proceed to flag if it does not.
    post_rounding = _strip_simple_latex(explanation[rounding_match.end():])
    post_matches = list(_STANDALONE_VAL_RE.finditer(post_rounding))
    if post_matches and match_answer_to_options(post_matches[-1].group(1), row):
        return []

    return [
        make_issue(
            "Critical",
            "Answer Not in Options",
            "Options",
            f"Computed result before rounding: {computed_val_str}",
            f"The explanation computes {computed_val_str} but then rounds to a different value. "
            f"The actual computed answer ({computed_val_str}) does not appear in any option — "
            f"the options should include the correctly computed value or the method should be revised.",
            suggested_fix=(
                f"Add an option matching the computed value ({computed_val_str}) or revise the "
                f"problem so the computed answer aligns with an existing option."
            ),
            confidence="High",
        )
    ]


def _has_arithmetic_issue(arithmetic_issues: Optional[Iterable[ReviewIssue]]) -> bool:
    if not arithmetic_issues:
        return False
    return any(issue.error_type in ARITHMETIC_ERROR_TYPES for issue in arithmetic_issues)


def _check_explanation_key_conflict(row, explanation: str, key: str) -> List[ReviewIssue]:
    """When arithmetic is already wrong, do not suggest a new key from the wrong explanation.

    Still flag explicit conflicts such as: explanation concludes 16 / Option D while
    the provided key is Option C. In that case the explanation should be fixed, not
    the key blindly changed.
    """
    issues: List[ReviewIssue] = []

    final_option = extract_final_option(explanation)

    if final_option and final_option in VALID_KEYS:
        if final_option != key:
            issues.append(
                make_issue(
                    "Critical",
                    "Explanation-Key Conflict",
                    "Explanation, Key",
                    f"Explanation indicates {final_option}; Key is {key}",
                    f"The explanation's final answer indicates {final_option}, but the key is marked as {key}. Because the explanation has a calculation error, fix the explanation instead of changing the key blindly.",
                    confidence="High",
                )
            )
        # Explicit option reference is authoritative — skip text-based fallback checks.
        return issues

    answer_text = extract_final_answer_text(explanation)
    if not answer_text:
        return issues

    matches = match_answer_to_options(answer_text, row)
    if len(matches) == 1 and matches[0] != key:
        issues.append(
            make_issue(
                "Critical",
                "Explanation-Key Conflict",
                "Explanation, Key",
                f"Explanation answer: {answer_text}; Matching option: {matches[0]}; Key: {key}",
                f"The explanation's final answer matches {matches[0]}, but the key is marked as {key}. Because the explanation has a calculation error, fix the explanation instead of changing the key blindly.",
                confidence="High",
            )
        )
    elif len(matches) > 1 and key not in matches:
        issues.append(
            make_issue(
                "Critical",
                "Explanation-Key Conflict",
                "Explanation, Key",
                f"Explanation answer: {answer_text}; Matching options: {', '.join(matches)}; Key: {key}",
                f"The explanation's final answer matches {', '.join(matches)}, but the key is marked as {key}. Because the explanation has a calculation error, fix the explanation instead of changing the key blindly.",
                confidence="High",
            )
        )
    elif len(matches) == 0:
        simple_answer = answer_text.strip()
        if re.fullmatch(
            r"[-+]?\d+(?:\.\d+)?(?:\s+\d+/\d+)?|[-+]?\d+/\d+|[-+]?\d+(?:\.\d+)?%"
            r"|\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|[-+]?\d+(?:\.\d+)?\s+[A-Za-z]+s?"
            r"|[-+]?\d+/\d+\s+[A-Za-z]+s?",
            simple_answer,
        ):
            issues.append(
                make_issue(
                    "Critical",
                    "Answer Not in Options",
                    "Options",
                    f"Explanation answer: {answer_text}",
                    "The final answer from the explanation does not match any option.",
                    confidence="High",
                )
            )

    return issues



def _check_internal_option_conflict(explanation: str) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []
    final_option = extract_final_option(explanation)
    if not final_option:
        return issues

    support_patterns = [
        r"(?:which|that|this|it)\s+is\s+(Option\s+[ABCD])",
        r"matches\s+(Option\s+[ABCD])",
        r"corresponds\s+to\s+(Option\s+[ABCD])",
    ]
    supported_options: List[str] = []
    for pattern in support_patterns:
        supported_options.extend(re.findall(pattern, explanation, flags=re.IGNORECASE))
    supported_options = [opt.title().replace("Option ", "Option ") for opt in supported_options]
    if supported_options and supported_options[-1] != final_option:
        issues.append(
            make_issue(
                "Critical",
                "Final Conclusion Mismatch",
                "Explanation",
                f"Explanation supports {supported_options[-1]} but final line says {final_option}",
                "The explanation first maps the computed answer to one option, but the final conclusion gives a different option.",
                confidence="High",
            )
        )
    return issues


def _check_body_time_vs_conclusion(explanation: str) -> List[ReviewIssue]:
    """Detect when the explanation body claims a time result that contradicts the final answer.

    Catches the pattern where a simplified/wrong shortcut gives "they need 12 hours" early
    in the explanation while the correct final answer is "11 hours 40 minutes".
    Requires an explicit '= N hours' so bare "A takes 10 hours" phrasing is not matched.
    """
    final_m = re.search(
        r"(?:(?:hence|therefore|so),?\s+|\*{0,2}[Aa]nswer\s*:\*{0,2}\s*)([^\n]*)",
        explanation,
        re.IGNORECASE | re.MULTILINE,
    )
    if not final_m:
        return []
    # Use the LAST "N hours" mention within the conclusion line, not the first:
    # a conclusion sentence may state an intermediate figure before the true
    # final answer (e.g. "after working alone for 3 hours, the remaining time = 11 hours").
    hour_matches = list(re.finditer(r"(\d+)\s*hours?", final_m.group(1), re.IGNORECASE))
    if not hour_matches:
        return []
    final_hours = int(hour_matches[-1].group(1))

    body = explanation[: final_m.start()]
    body_clean = _strip_simple_latex(body)
    body_m = re.search(
        r"\b(?:need|require|will\s+take|takes?)[^.\n]*=\s*(\d+)\s*hours?[.!]?\s*$",
        body_clean,
        re.IGNORECASE | re.MULTILINE,
    )
    if not body_m:
        return []
    body_hours = int(body_m.group(1))
    if body_hours == final_hours:
        return []

    return [
        make_issue(
            "Critical",
            "Final Conclusion Mismatch",
            "Explanation",
            f"Body states {body_hours} hours; conclusion states {final_hours} hours",
            f"The explanation body concludes {body_hours} hours but the final answer states {final_hours} hours. These are contradictory.",
            confidence="High",
        )
    ]


def check_final_answer_and_key(row, arithmetic_issues: Optional[Iterable[ReviewIssue]] = None) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []
    explanation = cell_text(row, "Explanation")
    key = cell_text(row, "Key")

    if not explanation or key not in VALID_KEYS:
        return issues

    internal_conflicts = _check_internal_option_conflict(explanation)

    if not _has_arithmetic_issue(arithmetic_issues):
        issues.extend(_check_rounding_mismatch(row, explanation))

    issues.extend(_check_conclusion_vs_computation(explanation))
    issues.extend(_check_body_time_vs_conclusion(explanation))

    # If a deterministic arithmetic/unit error is already found, avoid trusting the
    # explanation's final answer for key correction. Still report explicit conflicts.
    if _has_arithmetic_issue(arithmetic_issues):
        return issues + internal_conflicts + _check_explanation_key_conflict(row, explanation, key)

    final_option = extract_final_option(explanation)
    issues.extend(internal_conflicts)

    if final_option and final_option in VALID_KEYS:
        if final_option != key:
            issues.append(
                make_issue(
                    "Critical",
                    "Key Mismatch",
                    "Key",
                    f"Explanation indicates {final_option}; Key is {key}",
                    f"Explanation indicates {final_option}, but the key is marked as {key}.",
                    suggested_fix=final_option,
                    confidence="High",
                )
            )
        # Explicit option reference is authoritative — skip text-based fallback checks.
        return issues

    answer_text = extract_final_answer_text(explanation)
    # Fallback: when no explicit "hence/answer is" marker exists, use the last bare
    # "= VALUE" at end of a line as the implied final answer.
    if not answer_text:
        answer_text = extract_last_result_value(explanation)
    if not answer_text:
        return issues

    matches = match_answer_to_options(answer_text, row)
    if len(matches) == 1:
        matched_option = matches[0]
        if matched_option != key:
            issues.append(
                make_issue(
                    "Critical",
                    "Key Mismatch",
                    "Key",
                    f"Explanation answer: {answer_text}; Matching option: {matched_option}; Key: {key}",
                    f"Explanation answer matches {matched_option}, but the key is marked as {key}.",
                    suggested_fix=matched_option,
                    confidence="High",
                )
            )
    elif len(matches) > 1:
        issues.append(
            make_issue(
                "Critical",
                "Multiple Correct Options",
                "Options",
                f"Explanation answer: {answer_text}; Matches: {', '.join(matches)}",
                "Explanation answer matches more than one option.",
                confidence="High",
            )
        )
    else:
        # Only flag answer-not-in-options for simple final answers. This avoids overclaiming on long phrases.
        simple_answer = answer_text.strip()
        if re.fullmatch(
            r"[-+]?\d+(?:\.\d+)?(?:\s+\d+/\d+)?|[-+]?\d+/\d+|[-+]?\d+(?:\.\d+)?%"
            r"|\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|[-+]?\d+(?:\.\d+)?\s+[A-Za-z]+s?"
            r"|[-+]?\d+/\d+\s+[A-Za-z]+s?",
            simple_answer,
        ):
            # Before flagging as critical, check if the numeric part alone matches an option.
            # If so, the answer is correct — options just omit the unit (e.g. "36 years" vs "36").
            num_only_match = re.match(r"(-?\d+(?:\.\d+)?(?:\s+\d+/\d+)?|-?\d+/\d+)", simple_answer)
            numeric_matches = match_answer_to_options(num_only_match.group(1), row) if num_only_match else []
            # Also skip when the value is a component inside a ratio option (e.g. "66" in "45:54:66")
            ratio_component = any(
                re.search(rf"(?:^|:)\s*{re.escape(simple_answer.strip())}\s*(?::|$)", cell_text(row, col))
                for col in ("Option A", "Option B", "Option C", "Option D")
            )
            if not numeric_matches and not ratio_component:
                issues.append(
                    make_issue(
                        "Critical",
                        "Answer Not in Options",
                        "Options",
                        f"Explanation answer: {answer_text}",
                        "The final answer from the explanation does not match any option.",
                        confidence="High",
                    )
                )

    return issues
