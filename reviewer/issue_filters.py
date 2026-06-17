import re
from typing import Iterable, List, Optional

from .constants import OPTION_COLUMNS, VALID_KEYS
from .models import ReviewIssue, make_issue
from .normalize import cell_text
from .option_equivalence import match_answer_to_options, normalize_exact

GENDERED_RELATIONS = {
    "father", "mother", "brother", "sister", "son", "daughter", "uncle", "aunt",
    "niece", "nephew", "husband", "wife", "grandfather", "grandmother",
    "grandson", "granddaughter", "grandma", "grandpa",
}

# Opposite-gender pairs for option-constrained suppression.
_RELATION_GENDER_OPPOSITE = {
    "brother": "sister", "sister": "brother",
    "uncle": "aunt", "aunt": "uncle",
    "nephew": "niece", "niece": "nephew",
    "grandfather": "grandmother", "grandmother": "grandfather",
    "grandpa": "grandma", "grandma": "grandpa",
    "father": "mother", "mother": "father",
    "son": "daughter", "daughter": "son",
    "husband": "wife", "wife": "husband",
    "man": "woman", "boy": "girl",
}

# Gender-neutral umbrella terms and the gendered pair they resolve.
# E.g. when both "brother" and "sister" are options but the key is "sibling",
# the question is intentionally designed so gender cannot be determined and
# "sibling" is the correct answer — flag the ambiguity as a false positive.
_GENDER_NEUTRAL_TO_PAIR: dict = {
    "sibling": ("brother", "sister"),
    "parent": ("father", "mother"),
    "grandparent": ("grandfather", "grandmother"),
    "child": ("son", "daughter"),
    "spouse": ("husband", "wife"),
}


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _all_row_text(row) -> str:
    return " ".join(
        cell_text(row, col)
        for col in ["Question", "Option A", "Option B", "Option C", "Option D", "Explanation", "Key"]
    )


def _issue_text(issue: ReviewIssue) -> str:
    return f"{issue.error_type} {issue.evidence} {issue.reason} {issue.suggested_fix}".lower()


def _key_option_text(row) -> str:
    key = cell_text(row, "Key")
    return cell_text(row, key) if key in VALID_KEYS else ""


def _clone_issue_without_fix(issue: ReviewIssue) -> ReviewIssue:
    return make_issue(
        severity=issue.severity,
        error_type=issue.error_type,
        field=issue.field,
        evidence=issue.evidence,
        reason=issue.reason,
        suggested_fix="",
        confidence=issue.confidence,
    )


# ---------------------------------------------------------------------------
# Known false-positive suppression
# ---------------------------------------------------------------------------


def _is_ranking_or_position_row(text: str) -> bool:
    lower = text.lower()
    return any(
        term in lower
        for term in [
            "rank", "position", "from the left", "from the right", "from the top",
            "from the bottom", "from the front", "row", "queue", "line of",
            "interchange", "interchanged", "shifted", "ranks below", "ranks above",
        ]
    )


def _is_interchange_between_or_shift_row(text: str) -> bool:
    lower = text.lower()
    return any(
        term in lower
        for term in [
            "between", "interchange", "interchanged", "swapped", "shifted",
            "shift", "places to the left", "places to the right", "ranks below", "ranks above",
            "after interchanging", "after interchange",
        ]
    )


def _should_suppress_ranking_issue(row, issue: ReviewIssue) -> bool:
    row_text = _all_row_text(row)
    lower = row_text.lower()
    if not _is_ranking_or_position_row(lower):
        return False

    issue_lower = _issue_text(issue)

    # Do not let the LLM reject accepted two-person/interchange/between/shift items
    # by forcing a one-person both-end formula.
    if issue.error_type == "Formula Error" and _is_interchange_between_or_shift_row(lower):
        if any(
            phrase in issue_lower
            for phrase in [
                "left + right - 1", "subtract 1", "subtracted 1", "total should",
                "formula incorrectly", "contradiction", "retains", "original position",
                "interchange", "shift", "ranks below", "ranks above",
            ]
        ):
            return True

    # Do not reject a normal ranking question merely because a name/gender was used
    # in a mixed boys/girls context; content teams often accept identity from context.
    if issue.error_type == "Invalid Assumption" and "gender" in issue_lower:
        if any(term in lower for term in ["boys", "girls", "class of", "ranked"]):
            return True

    return False


def _is_clock_gain_loss_row(text: str) -> bool:
    lower = text.lower()
    return "clock" in lower and any(term in lower for term in ["loses", "loss", "gains", "gain", "slow", "fast"])


def _should_suppress_clock_gain_loss_issue(row, issue: ReviewIssue) -> bool:
    if not _is_clock_gain_loss_row(_all_row_text(row)):
        return False
    issue_lower = _issue_text(issue)
    if issue.error_type in {"Formula Error", "Unit Conversion Error", "Needs Human Review"}:
        return any(term in issue_lower for term in ["elapsed", "unit", "minutes", "hours", "indicated", "true time"])
    return False


def _is_blood_relation_row(text: str) -> bool:
    lower = text.lower()
    return any(word in lower for word in ["mother", "father", "brother", "sister", "sibling", "parent", "child", "niece", "nephew", "uncle", "aunt", "relation"])


_GENDERED_WORDS_ALL = {
    "father", "mother", "brother", "sister", "son", "daughter",
    "uncle", "aunt", "niece", "nephew", "husband", "wife",
    "grandfather", "grandmother", "grandson", "granddaughter",
    "man", "woman", "boy", "girl", "male", "female",
}


def _extract_claimed_person_name(issue: ReviewIssue) -> Optional[str]:
    """Return the name of the person whose gender the issue is asserting."""
    match = re.match(r"\b([A-Z][a-z]+)\b", issue.evidence)
    return match.group(1) if match else None


def _question_explicitly_genders_person(question_lower: str, name: str) -> bool:
    """Return True only when the question directly states a gendered relation for this person.

    Accepted patterns:
      - "Name is a brother/sister/uncle/aunt/..." (direct assertion)
      - "Name is his/her ..." — gendered possessive in a naming context
      - Gendered pronoun within a tight window after the name

    Rejected: "niece of Name" — that describes someone else's gender, not Name's.
    """
    n = re.escape(name.lower())
    for word in _GENDERED_WORDS_ALL:
        w = re.escape(word)
        if re.search(rf"\b{n}\b\s+(?:is|was)\s+(?:a\s+|an\s+)?{w}\b", question_lower):
            return True
    for pronoun in ["he", "him", "his", "she", "her", "hers"]:
        p = re.escape(pronoun)
        if re.search(rf"\b{n}\b[^.!?\n]{{0,25}}\b{p}\b", question_lower):
            return True
        if re.search(rf"\b{p}\b[^.!?\n]{{0,25}}\b{n}\b", question_lower):
            return True
    return False


def _opposite_gender_absent_from_options(row, key_option: str) -> bool:
    """Return True if the gender-opposite of key_option does not appear in any option.

    Example: key_option = "brother", opposite = "sister".
    If no option contains "sister", gender is uniquely determined by the option set.
    """
    opposite = _RELATION_GENDER_OPPOSITE.get(key_option.lower())
    if not opposite:
        return False
    for col in OPTION_COLUMNS:
        opt_text = cell_text(row, col).lower()
        if opposite in opt_text:
            return False  # Opposite gender IS present — ambiguity remains
    return True  # Opposite not present anywhere → options pin the gender


def _key_is_gender_neutral_resolution(row, key_option: str, issue: ReviewIssue) -> bool:
    """Return True when the key is a gender-neutral term (Sibling/Parent/Grandparent/etc.)
    that intentionally resolves a gender ambiguity.

    Triggered when:
    - The key option is a known gender-neutral umbrella (Sibling, Parent, Grandparent…), AND
    - Either the issue text mentions one of the gendered variants (brother/sister, etc.)
      OR both gendered variants appear among the options.

    This covers the pattern: options = [Brother, Sister, Grandpa, Sibling] → key = Sibling.
    The question is well-designed: gender cannot be determined, so the neutral term is correct.
    """
    neutral_pair = _GENDER_NEUTRAL_TO_PAIR.get(key_option.lower())
    if not neutral_pair:
        return False
    gendered_a, gendered_b = neutral_pair

    # Issue text explicitly mentions the gendered variants that are ambiguous
    issue_lower = _issue_text(issue)
    if gendered_a in issue_lower or gendered_b in issue_lower:
        return True

    # Both gendered variants appear in the option set → question designed for neutral answer
    option_texts = [cell_text(row, col).lower() for col in OPTION_COLUMNS]
    has_both = (
        any(gendered_a in t for t in option_texts)
        and any(gendered_b in t for t in option_texts)
    )
    return has_both


def _should_suppress_option_constrained_blood_issue(row, issue: ReviewIssue) -> bool:
    """Suppress over-strict blood-relation ambiguity when the options or key resolve it.

    Suppression conditions (any one is enough):
    (a) The question explicitly states the gender of the person named in the issue.
    (b) The options do not include the opposite-gender relation, uniquely pinning gender.
        Example: only "Brother" option, no "Sister" → must be Brother.
    (c) The key is a gender-neutral umbrella term (Sibling/Parent/Grandparent) that
        intentionally covers both gendered variants — both are present in options.
        Example: [Brother, Sister, Grandpa, Sibling], key = Sibling → suppress.
    """
    row_text = _all_row_text(row)
    lower = row_text.lower()
    if not _is_blood_relation_row(lower):
        return False
    if issue.error_type not in {"Blood Relation Ambiguity", "Invalid Assumption", "Ambiguity", "Needs Human Review"}:
        return False
    issue_lower = _issue_text(issue)
    if not any(term in issue_lower for term in ["gender", "assume", "ambig", "brother", "sister", "unique"]):
        return False

    key_option = _key_option_text(row).strip().lower()
    if not key_option or "cannot" in key_option or "not determined" in key_option:
        return False

    # Condition (c): key is a gender-neutral resolution term.
    if _key_is_gender_neutral_resolution(row, key_option, issue):
        return True

    # Conditions (a) and (b) only apply when key is itself a gendered relation.
    if key_option not in GENDERED_RELATIONS:
        return False

    # Condition (b): opposite gender relation absent from all options.
    if _opposite_gender_absent_from_options(row, key_option):
        return True

    # Condition (a): question explicitly states gender for the person named in the issue.
    claimed_name = _extract_claimed_person_name(issue)
    if not claimed_name:
        return False
    question_lower = cell_text(row, "Question").lower()
    return _question_explicitly_genders_person(question_lower, claimed_name)


def _has_alligation_root_issue(issues: Iterable[ReviewIssue]) -> bool:
    return any(
        issue.error_type in {"Formula Error", "Key Mismatch"}
        and "alligation" in _issue_text(issue)
        for issue in issues
    )


def _has_side_mismatch_issue(issues: Iterable[ReviewIssue]) -> bool:
    return any(
        issue.error_type == "Question-Explanation Mismatch"
        and any(term in _issue_text(issue) for term in ["opposite side", "right-side count", "left-side count", "students to the right"])
        for issue in issues
    )


def _has_coordinate_trace_issue(issues: Iterable[ReviewIssue]) -> bool:
    return any("coordinate trace" in _issue_text(issue) for issue in issues)


def _should_suppress_formula_when_side_mismatch_exists(row, issue: ReviewIssue, has_side_mismatch: bool) -> bool:
    if not has_side_mismatch or issue.error_type != "Formula Error":
        return False
    lower = _all_row_text(row).lower()
    if not _is_ranking_or_position_row(lower):
        return False
    issue_lower = _issue_text(issue)
    return any(term in issue_lower for term in ["left", "right", "students", "row", "position"])


def _should_suppress_direction_llm_duplicate(issue: ReviewIssue, has_coordinate_trace: bool) -> bool:
    if not has_coordinate_trace:
        return False
    issue_lower = _issue_text(issue)
    if "coordinate trace" in issue_lower:
        return False
    if issue.error_type in {"Final Conclusion Mismatch", "Formula Error", "Wrong Reasoning", "Question-Explanation Mismatch"}:
        return any(term in issue_lower for term in ["north", "south", "east", "west", "direction", "path", "walk"])
    return False


def _strip_risky_suggested_fix(row, issue: ReviewIssue) -> ReviewIssue:
    text = _all_row_text(row).lower()
    complex_terms = [
        "rank", "position", "row", "queue", "interchange", "shifted", "clock",
        "direction", "north", "south", "east", "west", "blood relation", "relation",
        "work in", "days", "alligation", "mixture", "years old", "age",
    ]
    # Keep Key Mismatch suggested fixes intact so Reviewed Key can be populated.
    if issue.suggested_fix and issue.error_type in {"Formula Error", "Wrong Substitution", "Final Conclusion Mismatch", "Question-Explanation Mismatch"}:
        if any(term in text for term in complex_terms):
            return _clone_issue_without_fix(issue)
    return issue


# ---------------------------------------------------------------------------
# Consequence enrichment: fill missing Key Mismatch when the corrected option is clear
# ---------------------------------------------------------------------------


def _option_value_mentioned(text: str, option_value: str) -> bool:
    if not text or not option_value:
        return False
    norm_option = normalize_exact(option_value)
    norm_text = normalize_exact(text)
    if len(norm_option) >= 2 and norm_option in norm_text:
        return True
    # For short options like Yes/No, require answer-style wording to avoid matching
    # normal prose such as "not" or "no issue".
    option_lower = option_value.strip().lower()
    if option_lower in {"yes", "no"}:
        return bool(re.search(rf"\b(?:answer|correct\s+answer|conclusion)\s+(?:is|=|:)\s+{re.escape(option_lower)}\b", text, flags=re.IGNORECASE))
    return False


def _infer_correct_option_from_issue(row, issue: ReviewIssue) -> Optional[str]:
    # Use suggested_fix and reason only. Evidence often contains the wrong original
    # answer, so using evidence creates false Key Mismatch labels.
    text = f"{issue.suggested_fix} {issue.reason}"
    label_matches = []
    for label in re.findall(r"\bOption\s+[ABCD]\b", text, flags=re.IGNORECASE):
        normalized = label.title()
        if normalized not in label_matches:
            label_matches.append(normalized)
    if len(label_matches) == 1:
        return label_matches[0]

    value_matches: List[str] = []
    for option_col in OPTION_COLUMNS:
        option_value = cell_text(row, option_col)
        if _option_value_mentioned(text, option_value):
            value_matches.append(option_col)

    # Also let the existing option matcher parse a few explicit answer snippets.
    snippet_patterns = [
        r"correct(?:ed)?\s+(?:answer|value|angle|count|fraction|percentage|result)?\s*(?:is|=|:)\s*([^.;\n]+)",
        r"should\s+be\s+([^.;\n]+)",
    ]
    for pattern in snippet_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = match.group(1).strip()
            for opt in match_answer_to_options(candidate, row):
                if opt not in value_matches:
                    value_matches.append(opt)

    unique = list(dict.fromkeys(value_matches))
    if len(unique) == 1:
        return unique[0]
    return None


def _add_missing_key_mismatch(row, issues: List[ReviewIssue]) -> List[ReviewIssue]:
    if any(issue.error_type == "Key Mismatch" for issue in issues):
        return issues
    key = cell_text(row, "Key")
    if key not in VALID_KEYS:
        return issues

    for issue in issues:
        if issue.severity != "Critical":
            continue
        if issue.error_type not in {"Formula Error", "Wrong Substitution", "Final Conclusion Mismatch", "Question-Explanation Mismatch"}:
            continue
        corrected = _infer_correct_option_from_issue(row, issue)
        if corrected and corrected != key:
            issues.append(
                make_issue(
                    "Critical",
                    "Key Mismatch",
                    "Key",
                    f"Corrected option inferred from review issue: {corrected}; Key: {key}",
                    "The review issue identifies a corrected answer that maps to a different option than the marked key.",
                    suggested_fix=corrected,
                    confidence="High",
                )
            )
            break
    return issues


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def filter_known_false_positives(row, issues: Iterable[ReviewIssue]) -> List[ReviewIssue]:
    """Final precision gate for repeated validation patterns.

    It removes known false-positive families, strips risky LLM corrections, and
    adds a Key Mismatch consequence only when the corrected option is unambiguous.
    """
    original = list(issues)
    has_alligation_issue = _has_alligation_root_issue(original)
    has_side_mismatch = _has_side_mismatch_issue(original)
    has_coordinate_trace = _has_coordinate_trace_issue(original)
    # When Wrong Substitution is the root cause, Calculation Errors describing the
    # arithmetic of those wrong values are secondary artifacts — not independently useful.
    has_wrong_substitution = any(i.error_type == "Wrong Substitution" for i in original)
    # When the correct answer isn't in any option, a Key Mismatch pointing to an option
    # derived from the wrong values is misleading.
    has_answer_not_in_options = any(i.error_type == "Answer Not in Options" for i in original)
    filtered: List[ReviewIssue] = []

    for issue in original:
        if has_alligation_issue and issue.error_type == "Calculation Error":
            continue
        if has_wrong_substitution and issue.error_type == "Calculation Error":
            continue
        if has_answer_not_in_options and issue.error_type == "Key Mismatch":
            continue
        if _should_suppress_ranking_issue(row, issue):
            continue
        if _should_suppress_clock_gain_loss_issue(row, issue):
            continue
        if _should_suppress_option_constrained_blood_issue(row, issue):
            continue
        if _should_suppress_formula_when_side_mismatch_exists(row, issue, has_side_mismatch):
            continue
        if _should_suppress_direction_llm_duplicate(issue, has_coordinate_trace):
            continue
        filtered.append(_strip_risky_suggested_fix(row, issue))

    return _add_missing_key_mismatch(row, filtered)
