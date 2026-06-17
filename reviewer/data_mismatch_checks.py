import re
from typing import Dict, List, Optional, Tuple

from .constants import VALID_KEYS
from .models import ReviewIssue, make_issue
from .normalize import cell_text
from .option_equivalence import match_answer_to_options

DIRECTIONS = "left|right|top|bottom|front|back"


def _rank_facts(text: str) -> Dict[Tuple[str, str], str]:
    facts: Dict[Tuple[str, str], str] = {}
    pattern = re.compile(
        rf"\b([A-Z][a-z]+)\b[^.\n]{{0,40}}?\b(\d+)(?:st|nd|rd|th)?\s+from\s+(?:the\s+)?({DIRECTIONS})",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        name = match.group(1).lower()
        number = match.group(2)
        direction = match.group(3).lower()
        facts[(name, direction)] = number
    return facts


def _name_position_direction_facts(text: str) -> Dict[str, Tuple[str, str]]:
    facts: Dict[str, Tuple[str, str]] = {}
    pattern = re.compile(
        rf"\b([A-Z][a-z]+)\b[^.\n]{{0,40}}?\b(\d+)(?:st|nd|rd|th)?\s+from\s+(?:the\s+)?({DIRECTIONS})",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        facts[match.group(1).lower()] = (match.group(2), match.group(3).lower())
    return facts


def _question_target_direction(question: str) -> Optional[str]:
    patterns = [
        rf"find[^.\n]{{0,60}}?\b(?:position|rank)\s+from\s+(?:the\s+)?({DIRECTIONS})",
        rf"find\s+(?:the\s+)?(?:number\s+of\s+)?(?:students|persons|people|boys|girls)\s+to\s+(?:his|her|their|the)?\s*({DIRECTIONS})",
        rf"how\s+many\s+(?:students|persons|people|boys|girls)\s+(?:are\s+)?(?:to|on)\s+(?:his|her|their|the)?\s*({DIRECTIONS})",
        # "what is her/his/X's position/rank from the right/left/..."
        rf"what\s+(?:is|was)[^.\n]{{0,80}}?\b(?:position|rank)\s+from\s+(?:the\s+)?({DIRECTIONS})",
        # "position from right end" at end of sentence as a question
        rf"\b(?:position|rank)\s+from\s+(?:the\s+)?({DIRECTIONS})\s*(?:end)?\s*\?",
    ]
    for pattern in patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            # findall returns the first non-None group across all groups
            for g in match.groups():
                if g:
                    return g.lower()
    return None


def _explanation_target_direction(explanation: str) -> Optional[str]:
    patterns = [
        rf"\b(?:position|rank)\s+from\s+(?:the\s+)?({DIRECTIONS})\s*=",
        rf"\b(?:students|persons|people|boys|girls)\s+to\s+(?:his|her|their|the)?\s*({DIRECTIONS})\s*=",
        rf"\b(?:to|on)\s+(?:his|her|their|the)?\s*({DIRECTIONS})\s*=",
    ]
    # Use the LAST match so intermediate computation lines (e.g. "Rank from top = 25")
    # don't shadow the final answer direction (e.g. "Rank from bottom = ...").
    last_match = None
    last_pos = -1
    for pattern in patterns:
        for match in re.finditer(pattern, explanation, flags=re.IGNORECASE):
            if match.start() > last_pos:
                last_pos = match.start()
                last_match = match.group(1).lower()
    return last_match


def _left_right_positions_from_question(question: str) -> Optional[Tuple[int, int]]:
    """Return both-end positions only when they clearly belong to the same person.

    The formula total = left + right - 1 applies to one person's position from
    both ends. It should not be applied to two-person questions with words like
    "between", "interchange", or "shifted".
    """
    lower = question.lower()
    if any(word in lower for word in ["between", "interchange", "interchanged", "shift", "shifted", "swapped", "swap"]):
        return None

    # Handles: Priya is 9th from the left and 14th from the right.
    pattern = re.compile(
        r"\b([A-Z][a-z]+)\b[^.\n]{0,80}?\b(\d+)(?:st|nd|rd|th)?\s+from\s+(?:the\s+)?(left|right)"
        r"[^.\n]{0,80}?\b(?:\1\b[^.\n]{0,40}?)?(\d+)(?:st|nd|rd|th)?\s+from\s+(?:the\s+)?(left|right)",
        flags=re.IGNORECASE,
    )
    match = pattern.search(question)
    if not match:
        return None
    n1, d1, n2, d2 = int(match.group(2)), match.group(3).lower(), int(match.group(4)), match.group(5).lower()
    if {d1, d2} != {"left", "right"}:
        return None
    left = n1 if d1 == "left" else n2
    right = n1 if d1 == "right" else n2
    return left, right


def _looks_like_total_count_question(question: str) -> bool:
    return bool(
        re.search(r"how\s+many\s+(?:students|persons|people|boys|girls)", question, flags=re.IGNORECASE)
        or re.search(r"total\s+(?:students|persons|people|boys|girls)", question, flags=re.IGNORECASE)
    )


def _extract_stated_total_from_explanation(explanation: str) -> Optional[int]:
    patterns = [
        r"total\s+(?:students|persons|people|boys|girls)?\s*=\s*[^.\n]*?=\s*(\d+)",
        r"correct\s+answer\s+(?:is|=|:)\s*(\d+)",
        r"answer\s+(?:is|=|:)\s*(\d+)",
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, explanation, flags=re.IGNORECASE))
        if matches:
            return int(matches[-1].group(1))
    return None


def _add_key_mismatch_for_correct_value(issues: List[ReviewIssue], row, corrected_value: int, evidence: str) -> None:
    key = cell_text(row, "Key")
    if key not in VALID_KEYS:
        return
    matches = match_answer_to_options(str(corrected_value), row)
    if len(matches) == 1 and matches[0] != key:
        issues.append(
            make_issue(
                "Critical",
                "Key Mismatch",
                "Key",
                f"Corrected value: {corrected_value}; Matching option: {matches[0]}; Key: {key}",
                f"After applying the correct ranking formula, the answer is {corrected_value}, which matches {matches[0]}, but the key is marked as {key}.",
                suggested_fix=matches[0],
                confidence="High",
            )
        )


def _position_direction_to_name(text: str) -> Dict[Tuple[str, str], str]:
    """Return a map of (position_number, direction) → person_name (lowercase).

    Used to detect name swaps: when the explanation attributes Person A's
    position to Person B.
    Handles two formats:
      - "NAME is 8th from the left"  (number before direction)
      - "NAME position from left is 8"  (number after direction)
    """
    facts: Dict[Tuple[str, str], str] = {}
    # Format 1: number before direction — "Kiran is 21st from the right"
    pattern1 = re.compile(
        rf"\b([A-Z][a-z]+)\b[^.\n]{{0,40}}?\b(\d+)(?:st|nd|rd|th)?\s+from\s+(?:the\s+)?({DIRECTIONS})",
        flags=re.IGNORECASE,
    )
    for match in pattern1.finditer(text):
        key = (match.group(2), match.group(3).lower())
        facts[key] = match.group(1).lower()
    # Format 2: number after direction — "Mohan position from left is 8"
    pattern2 = re.compile(
        rf"\b([A-Z][a-z]+)\b[^.\n]{{0,60}}?\bfrom\s+(?:the\s+)?({DIRECTIONS})\b[^.\n]{{0,30}}?\bis\s+(\d+)",
        flags=re.IGNORECASE,
    )
    for match in pattern2.finditer(text):
        key = (match.group(3), match.group(2).lower())
        facts[key] = match.group(1).lower()
    return facts


_INTERCHANGE_WORDS = {"interchange", "interchanged", "swap", "swapped", "shift", "shifted"}


def _is_interchange_question(question: str) -> bool:
    lower = question.lower()
    return any(w in lower for w in _INTERCHANGE_WORDS)


def _check_name_swap(question: str, explanation: str) -> List[ReviewIssue]:
    """Flag when the explanation uses Person A's (position, direction) for Person B."""
    # In interchange/swap questions the explanation intentionally reassigns
    # positions after the swap — skip name-swap detection entirely.
    if _is_interchange_question(question):
        return []
    q_pd = _position_direction_to_name(question)
    e_pd = _position_direction_to_name(explanation)
    q_names = set(q_pd.values())

    issues: List[ReviewIssue] = []
    seen_pairs: set = set()
    for (pos, direction), q_name in q_pd.items():
        if (pos, direction) not in e_pd:
            continue
        e_name = e_pd[(pos, direction)]
        if e_name == q_name:
            continue
        if e_name not in q_names:
            continue
        pair = tuple(sorted([q_name, e_name]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        issues.append(
            make_issue(
                "Critical",
                "Data Mismatch",
                "Explanation",
                f"Question: {q_name.title()} is {pos} from {direction}; Explanation: {e_name.title()} is {pos} from {direction}",
                f"The explanation attributes {q_name.title()}'s position ({pos} from {direction}) to {e_name.title()} instead. The names appear to be swapped.",
                confidence="High",
            )
        )
    return issues


_COMMON_WORDS = {
    "total", "according", "required", "hence", "therefore", "since", "given",
    "let", "now", "note", "find", "also", "from", "the", "in", "at", "for",
    "by", "with", "of", "to", "an", "be", "is", "are", "was", "were", "has",
    "have", "had", "can", "will", "would", "could", "should", "may", "might",
    "rank", "position", "row", "left", "right", "top", "bottom", "front", "back",
    "new", "original", "correct", "true", "false", "here", "there", "then",
    "answer", "option", "explanation", "question", "boys", "girls", "students",
    "persons", "people", "number", "place", "end", "side", "line", "queue",
    # Conjunctions / sentence starters that get capitalised mid-explanation
    "and", "but", "so", "yet", "nor", "after", "before", "when", "where",
    "which", "that", "this", "these", "those", "thus", "next", "also", "hence",
    "because", "since", "while", "although", "however", "therefore", "moreover",
    "note", "here", "now", "then", "using", "applying",
    # Action words mistaken for names in interchange/swap questions
    "interchange", "interchanged", "swap", "swapped", "shift", "shifted",
}


def _structural_names_from_text(text: str) -> set:
    """Return proper names used in ranking/position context (not common words)."""
    # Names in "NAME position from DIRECTION" or "NAME is Nth from DIRECTION"
    pattern = re.compile(
        rf"\b([A-Z][a-z]{{2,}})\b(?:[^.\n]{{0,60}}?\bfrom\s+(?:the\s+)?(?:{DIRECTIONS})|[^.\n]{{0,40}}?\b\d+(?:st|nd|rd|th)?\s+from\s+(?:the\s+)?(?:{DIRECTIONS}))",
        flags=re.IGNORECASE,
    )
    names = set()
    for match in pattern.finditer(text):
        name = match.group(1)
        if name.lower() not in _COMMON_WORDS:
            names.add(name.lower())
    return names


def _check_person_name_substitution(question: str, explanation: str) -> List[ReviewIssue]:
    """Flag when the explanation uses a name not present in the question in a position context.

    Example: question mentions Roshini but explanation uses Raji.
    """
    if _is_interchange_question(question):
        return []
    e_names = _structural_names_from_text(explanation)
    if not e_names:
        return []
    q_text_lower = question.lower()
    issues = []
    seen = set()
    for name in e_names:
        if re.search(rf"\b{re.escape(name)}\b", q_text_lower):
            continue  # Name IS in the question — no problem
        # Name in explanation but not in question. Find what name the question uses.
        q_names = _structural_names_from_text(question)
        conflicting = [qn for qn in q_names if not re.search(rf"\b{re.escape(qn)}\b", explanation.lower())]
        pair = (name, tuple(sorted(conflicting)))
        if pair in seen:
            continue
        seen.add(pair)
        if conflicting:
            q_name_str = ", ".join(qn.title() for qn in conflicting)
            issues.append(
                make_issue(
                    "Critical",
                    "Data Mismatch",
                    "Explanation",
                    f"Explanation uses '{name.title()}'; Question mentions '{q_name_str}'",
                    f"The explanation uses '{name.title()}' but the question refers to '{q_name_str}'. The wrong name is used in the explanation.",
                    confidence="High",
                )
            )
        else:
            issues.append(
                make_issue(
                    "Critical",
                    "Data Mismatch",
                    "Explanation",
                    f"Explanation uses '{name.title()}' which does not appear in the question",
                    f"The explanation uses '{name.title()}' but this name does not appear in the question.",
                    confidence="High",
                )
            )
    return issues


def _check_gender_word_swap(question: str, explanation: str) -> List[ReviewIssue]:
    """Flag when the question says 'girls' but the explanation uses 'boys' as the group label (or vice versa)."""
    q = question.lower()
    e = explanation.lower()

    q_girls = bool(re.search(r"\bgirls\b", q))
    q_boys = bool(re.search(r"\bboys\b", q))
    # Only fire when the question clearly refers to one gender exclusively.
    if q_girls == q_boys:
        return []

    total_re = re.compile(r"\btotal\s+no\.?\s*of\s+(boys|girls)\b|\bno\.?\s*of\s+(boys|girls)\b|\bnumber\s+of\s+(boys|girls)\b", re.IGNORECASE)
    e_gender_labels = {m.group(1) or m.group(2) or m.group(3) for m in total_re.finditer(e)}

    issues: List[ReviewIssue] = []
    if q_girls and any(g.lower() == "boys" for g in e_gender_labels):
        issues.append(
            make_issue(
                "Critical",
                "Data Mismatch",
                "Explanation",
                "Question refers to girls but explanation uses 'boys' in the count/total.",
                "The question specifies girls, but the explanation labels the group as boys.",
                confidence="High",
            )
        )
    if q_boys and any(g.lower() == "girls" for g in e_gender_labels):
        issues.append(
            make_issue(
                "Critical",
                "Data Mismatch",
                "Explanation",
                "Question refers to boys but explanation uses 'girls' in the count/total.",
                "The question specifies boys, but the explanation labels the group as girls.",
                confidence="High",
            )
        )
    return issues


def _check_ranking_total_formula(row, question: str, explanation: str) -> List[ReviewIssue]:
    issues: List[ReviewIssue] = []
    if not _looks_like_total_count_question(question):
        return issues
    positions = _left_right_positions_from_question(question)
    if not positions:
        return issues
    left, right = positions
    correct_total = left + right - 1
    stated_total = _extract_stated_total_from_explanation(explanation)
    if stated_total is None or stated_total == correct_total:
        return issues

    issues.append(
        make_issue(
            "Critical",
            "Formula Error",
            "Explanation",
            f"Left position: {left}; Right position: {right}; Explanation total: {stated_total}",
            f"For positions from both ends, total should be left position + right position - 1. Correct total is {correct_total}, not {stated_total}.",
            suggested_fix=f"Total students = {left} + {right} - 1 = {correct_total}",
            confidence="High",
        )
    )
    _add_key_mismatch_for_correct_value(issues, row, correct_total, explanation)
    return issues


def check_data_mismatch(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    issues: List[ReviewIssue] = []

    if not question or not explanation:
        return issues

    question_rank = _rank_facts(question)
    explanation_rank = _rank_facts(explanation)
    for key, q_number in question_rank.items():
        if key in explanation_rank and explanation_rank[key] != q_number:
            name, direction = key
            issues.append(
                make_issue(
                    "Critical",
                    "Data Mismatch",
                    "Explanation",
                    f"Question: {name} {q_number} from {direction}; Explanation: {name} {explanation_rank[key]} from {direction}",
                    f"The explanation uses a different position for {name.title()} from the {direction}.",
                    confidence="High",
                )
            )

    q_by_name = _name_position_direction_facts(question)
    e_by_name = _name_position_direction_facts(explanation)
    for name, (q_number, q_direction) in q_by_name.items():
        if name in e_by_name:
            e_number, e_direction = e_by_name[name]
            if q_number == e_number and q_direction != e_direction:
                issues.append(
                    make_issue(
                        "Critical",
                        "Data Mismatch",
                        "Explanation",
                        f"Question: {name} {q_number} from {q_direction}; Explanation: {name} {e_number} from {e_direction}",
                        f"The explanation changes {name.title()}'s direction from {q_direction} to {e_direction}.",
                        confidence="High",
                    )
                )

    q_target = _question_target_direction(question)
    e_target = _explanation_target_direction(explanation)
    if q_target and e_target and q_target != e_target:
        issues.append(
            make_issue(
                "Critical",
                "Direction Mismatch",
                "Explanation",
                f"Question asks for {q_target}; Explanation solves for {e_target}",
                f"The question asks for the {q_target} side/direction, but the explanation solves for the {e_target} side/direction.",
                confidence="High",
            )
        )

    issues.extend(_check_ranking_total_formula(row, question, explanation))
    issues.extend(_check_name_swap(question, explanation))
    issues.extend(_check_person_name_substitution(question, explanation))
    issues.extend(_check_gender_word_swap(question, explanation))

    return issues
