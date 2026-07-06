import re
from typing import List, Set

from .models import ReviewIssue, make_issue
from .normalize import cell_text

# Names strongly associated with a specific gender in Indian aptitude questions.
# Keep conservative — only include names with very strong and consistent gender association.
KNOWN_FEMALE_NAMES = {
    "raji", "priya", "divya", "kavya", "meena", "rekha", "seema", "anita",
    "sunita", "lakshmi", "radha", "sita", "gita", "geeta", "sneha", "pooja",
    "puja", "deepa", "swathi", "swati", "nisha", "asha", "usha", "veena",
    "meera", "sanya", "saniya", "rupa", "saritha", "kavitha", "savitha",
    "sangeetha", "sangeeta", "revathi", "revati", "nalini", "malini", "vasantha",
    "vasanta", "sumathi", "sumati", "saraswathi", "saraswati",
}

KNOWN_MALE_NAMES = {
    "raju", "mohan", "ramesh", "suresh", "rajesh", "arun", "arjun", "vijay",
    "harish", "dinesh", "ganesh", "mahesh", "rakesh", "naresh", "mukesh",
    "ritesh", "sathish", "satish", "girish", "manish", "ashok", "ashok",
    "anil", "sunil", "kapil", "akhil", "nikhil", "sanjay", "ajay", "vijay",
}

BLOOD_RELATION_WORDS = {
    "relation",
    "father",
    "mother",
    "brother",
    "sister",
    "sibling",
    "parent",
    "child",
    "son",
    "daughter",
    "uncle",
    "aunt",
    "niece",
    "nephew",
    "husband",
    "wife",
    "grandfather",
    "grandmother",
    "grandparent",
    "grandson",
    "granddaughter",
}

GENDER_WORDS = {
    "male",
    "female",
    "man",
    "woman",
    "boy",
    "girl",
    "father",
    "mother",
    "brother",
    "sister",
    "son",
    "daughter",
    "uncle",
    "aunt",
    "niece",
    "nephew",
    "husband",
    "wife",
    "grandfather",
    "grandmother",
}

PRONOUN_GROUPS = {
    "male": {"he", "him", "his"},
    "female": {"she", "her", "hers"},
}

NAME_TOKEN = r"[A-Z](?:[a-z]+)?"


def _contains_blood_relation(text: str) -> bool:
    lower = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lower) for word in BLOOD_RELATION_WORDS)


_COMMON_NON_NAME_WORDS = {
    "the", "aptitude", "option", "hence", "therefore", "so",
    # Question-starters: aptitude questions routinely begin a sentence with one
    # of these, capitalizing them the same way a real name would be capitalized.
    "what", "how", "who", "when", "where", "which", "why", "find", "given",
    "also", "now", "then", "consider", "suppose", "since", "while", "after",
    "before", "this", "that", "these", "those", "total", "rank", "position",
    "here", "there", "note", "let", "in", "on", "at", "for", "with", "by",
    "if", "an", "is", "are",
}


def _names(text: str) -> Set[str]:
    # Keep single-letter variables such as A, B, C, P, Q, R unconditionally — they
    # are common in blood-relation aptitude questions and can't be told apart from
    # the indefinite article "a" by casing alone.
    names: Set[str] = set()
    for token in re.findall(rf"\b{NAME_TOKEN}\b", text):
        if len(token) == 1:
            names.add(token)
            continue
        lower = token.lower()
        if lower in _COMMON_NON_NAME_WORDS:
            continue
        # Sentence-starter adverbs (Initially, Similarly, ...) get capitalized
        # mid-explanation but are not names — same rule used in data_mismatch_checks.py.
        if lower.endswith("ly") and len(lower) > 4:
            continue
        names.add(token)
    return names


def _is_only_name_in(name: str, text: str) -> bool:
    """True if `name` is the only person-name token found in `text` (case-insensitive).

    Nearest-antecedent guard: a pronoun should only be attributed to a known-gendered
    name when no other name is present that could be the real antecedent — e.g.
    "Priya's brother Ravi says he is happy" — Ravi, not Priya, is the closer and
    correct antecedent for "he", even though Priya is a known female name.
    """
    others = {n.lower() for n in _names(text)} - {name.lower()}
    return not others


def _explicit_gender_assertions(text: str):
    relation_words = "|".join(re.escape(word) for word in sorted(GENDER_WORDS, key=len, reverse=True))
    pattern = re.compile(
        rf"\b({NAME_TOKEN})\b\s+"
        rf"(?:is|was|being)\s+"
        rf"(?:a\s+|an\s+)?"
        rf"({relation_words})"
        rf"(?:\s+person)?\b",
        flags=re.IGNORECASE,
    )
    return [(m.group(1), m.group(2).lower(), m.group(0)) for m in pattern.finditer(text)]


def _question_has_explicit_gender_for_name(question: str, name: str, gender_or_relation: str) -> bool:
    target = name.lower()
    for q_name, q_gender, _ in _explicit_gender_assertions(question):
        if q_name.lower() == target and q_gender == gender_or_relation:
            return True
    return False


_RELATION_WORDS = {
    "father", "mother", "brother", "sister", "uncle", "aunt",
    "nephew", "niece", "grandson", "granddaughter", "grandfather",
    "grandmother", "son", "daughter", "husband", "wife", "cousin",
}

# Maps a specific relation to broader terms that appear in options
_RELATION_SYNONYMS: dict = {
    "sister": {"sibling"},
    "brother": {"sibling"},
    "grandmother": {"grandparent", "grandma", "gran"},
    "grandfather": {"grandparent", "grandpa"},
    "granddaughter": {"grandchild"},
    "grandson": {"grandchild"},
}

# "must be" is included so final sentences like "Charan must be Pooja's brother"
# are detected even when they don't start with hence/therefore/so/thus.
_CONCLUSION_PATTERN = re.compile(
    r"\b(?:hence|therefore|so|thus|must\s+be)\b[^.?!\n]{0,120}?\b("
    + "|".join(re.escape(w) for w in sorted(_RELATION_WORDS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _check_conclusion_vs_options(row) -> List[ReviewIssue]:
    """Flag when the explanation's final conclusion names a relation not in any option.

    Uses the LAST matching connector phrase so intermediate reasoning steps
    (e.g. 'so Pooja is also Lavanya's mother') are not mistaken for the answer.
    Also accepts synonym relations: 'sister' is compatible with 'Sibling'.
    """
    explanation = cell_text(row, "Explanation")
    if not explanation:
        return []

    # If no option contains a relation word, this is a numeric-answer question
    # (e.g. age or calculation), not a blood-relation question — skip.
    options_text = " ".join(
        cell_text(row, col).lower()
        for col in ("Option A", "Option B", "Option C", "Option D")
    )
    if not any(re.search(rf"\b{re.escape(w)}\b", options_text) for w in _RELATION_WORDS):
        return []

    # Take the LAST match — intermediate steps fire earlier, final conclusion fires last
    matches = list(_CONCLUSION_PATTERN.finditer(explanation))
    if not matches:
        return []
    match = matches[-1]

    conclusion = match.group(1).lower()

    # Check if exact word matches an option.
    exact_match = re.search(rf"\b{re.escape(conclusion)}\b", options_text)
    if exact_match:
        return []

    # Check if a gender-neutral synonym matches an option (e.g., "brother" → "Sibling").
    synonyms = _RELATION_SYNONYMS.get(conclusion, set())
    synonym_match = any(re.search(rf"\b{re.escape(w)}\b", options_text) for w in synonyms)
    if synonym_match:
        # The conclusion uses a gender-specific term but options use a gender-neutral synonym.
        # The explanation is making an unjustified gender assumption — flag it.
        return [
            make_issue(
                "Major",
                "Question-Explanation Mismatch",
                "Explanation",
                match.group(0),
                f"The explanation concludes '{conclusion}' (gender-specific) but the answer options use a "
                f"gender-neutral term. The question does not state this person's gender, so the explanation "
                f"should acknowledge the ambiguity and use the option's phrasing.",
                suggested_fix=(
                    f"Revise the conclusion: state that the person could be {conclusion} or [opposite gender], "
                    f"then conclude with the gender-neutral option label from the available choices."
                ),
                confidence="High",
            )
        ]

    return [
        make_issue(
            "Major",
            "Question-Explanation Mismatch",
            "Explanation",
            match.group(0),
            f"The explanation concludes '{conclusion}' but this relation does not appear in any answer option.",
            suggested_fix="Revise the explanation's conclusion to match one of the available options.",
            confidence="High",
        )
    ]


def check_gender_relation(row) -> List[ReviewIssue]:
    question = cell_text(row, "Question")
    explanation = cell_text(row, "Explanation")
    issues: List[ReviewIssue] = []

    if not question:
        return issues

    combined = f"{question} {explanation}"

    # General explicit pronoun mismatch outside/inside blood relations.
    male_named = set(
        re.findall(
            rf"\b({NAME_TOKEN})\b\s+(?:is|was)\s+(?:a\s+)?(?:boy|man|male)\b",
            question,
            flags=re.IGNORECASE,
        )
    )
    female_named = set(
        re.findall(
            rf"\b({NAME_TOKEN})\b\s+(?:is|was)\s+(?:a\s+)?(?:girl|woman|female)\b",
            question,
            flags=re.IGNORECASE,
        )
    )

    for name in male_named:
        window = re.search(rf"\b{name}\b[^.?!]*(\bshe\b|\bher\b|\bhers\b)", combined, flags=re.IGNORECASE)
        if window:
            issues.append(
                make_issue(
                    "Major",
                    "Pronoun/Gender Error",
                    "Question/Explanation",
                    window.group(0),
                    f"{name} is explicitly identified as male, but a female pronoun is used.",
                    confidence="High",
                )
            )

    for name in female_named:
        window = re.search(rf"\b{name}\b[^.?!]*(\bhe\b|\bhim\b|\bhis\b)", combined, flags=re.IGNORECASE)
        if window:
            issues.append(
                make_issue(
                    "Major",
                    "Pronoun/Gender Error",
                    "Question/Explanation",
                    window.group(0),
                    f"{name} is explicitly identified as female, but a male pronoun is used.",
                    confidence="High",
                )
            )

    # Check known-gendered names for pronoun mismatch even without explicit "is a man/woman" statement.
    all_names = _names(question) | _names(explanation or "")
    for name in all_names:
        lower = name.lower()
        if lower in KNOWN_FEMALE_NAMES:
            window = re.search(rf"\b{re.escape(name)}\b([^.?!]*)(\bhe\b|\bhim\b|\bhis\b)", combined, flags=re.IGNORECASE)
            if window:
                if _is_only_name_in(name, window.group(1)):
                    issues.append(
                        make_issue(
                            "Major",
                            "Pronoun/Gender Error",
                            "Question/Explanation",
                            window.group(0),
                            f"'{name}' is a female name, but a male pronoun (he/him/his) is used.",
                            confidence="Medium",
                        )
                    )
                # else: a closer name appears between `name` and the pronoun — that
                # name, not `name`, is the more likely antecedent. Do not flag.
            elif re.search(rf"\b{re.escape(name)}\b", question, re.IGNORECASE) and _is_only_name_in(name, question):
                # Cross-sentence: name and pronoun in separate sentences (e.g. "Priya is 5th. What is his rank?").
                # Only fire when this is the sole name of ANY kind in the question — if
                # another name is present, it could be the pronoun's real antecedent.
                question_known = [
                    n.lower() for n in _names(question)
                    if n.lower() in KNOWN_FEMALE_NAMES | KNOWN_MALE_NAMES
                ]
                if len(set(question_known)) == 1:
                    cross = re.search(r"\b(he|him|his)\b", question, re.IGNORECASE)
                    if cross:
                        issues.append(
                            make_issue(
                                "Major",
                                "Pronoun/Gender Error",
                                "Question",
                                cross.group(0),
                                f"'{name}' is a female name, but a male pronoun (he/him/his) is used.",
                                confidence="Medium",
                            )
                        )
        elif lower in KNOWN_MALE_NAMES:
            window = re.search(rf"\b{re.escape(name)}\b([^.?!]*)(\bshe\b|\bher\b|\bhers\b)", combined, flags=re.IGNORECASE)
            if window:
                if _is_only_name_in(name, window.group(1)):
                    issues.append(
                        make_issue(
                            "Major",
                            "Pronoun/Gender Error",
                            "Question/Explanation",
                            window.group(0),
                            f"'{name}' is a male name, but a female pronoun (she/her/hers) is used.",
                            confidence="Medium",
                        )
                    )
                # else: a closer name appears between `name` and the pronoun — that
                # name, not `name`, is the more likely antecedent. Do not flag.
            elif re.search(rf"\b{re.escape(name)}\b", question, re.IGNORECASE) and _is_only_name_in(name, question):
                # Cross-sentence: name and pronoun in separate sentences (e.g. "Suresh is 25th. What is her rank?").
                # Only fire when this is the sole name of ANY kind in the question — if
                # another name is present, it could be the pronoun's real antecedent.
                question_known = [
                    n.lower() for n in _names(question)
                    if n.lower() in KNOWN_FEMALE_NAMES | KNOWN_MALE_NAMES
                ]
                if len(set(question_known)) == 1:
                    cross = re.search(r"\b(she|her|hers)\b", question, re.IGNORECASE)
                    if cross:
                        issues.append(
                            make_issue(
                                "Major",
                                "Pronoun/Gender Error",
                                "Question",
                                cross.group(0),
                                f"'{name}' is a male name, but a female pronoun (she/her/hers) is used.",
                                confidence="Medium",
                            )
                        )

    # Check for unnamed gender references: "a man...her" or "a woman...he" within the same sentence.
    for m in re.finditer(r"(?:a|the)\s+(?:man|boy)\b([^.?!]*?)(\bshe\b|\bher\b|\bhers\b)", combined, flags=re.IGNORECASE):
        issues.append(
            make_issue(
                "Major",
                "Pronoun/Gender Error",
                "Question/Explanation",
                m.group(0),
                "The subject is referred to as a man/boy, but a female pronoun is used in the same sentence.",
                confidence="High",
            )
        )
    for m in re.finditer(r"(?:a|the)\s+(?:woman|girl)\b([^.?!]*?)(\bhe\b|\bhim\b|\bhis\b)", combined, flags=re.IGNORECASE):
        issues.append(
            make_issue(
                "Major",
                "Pronoun/Gender Error",
                "Question/Explanation",
                m.group(0),
                "The subject is referred to as a woman/girl, but a male pronoun is used in the same sentence.",
                confidence="High",
            )
        )

    if not _contains_blood_relation(combined):
        return issues

    issues.extend(_check_conclusion_vs_options(row))

    explanation_assertions = _explicit_gender_assertions(explanation)

    for name, gender, evidence in explanation_assertions:
        # Use case-insensitive search so all-lowercase questions are handled correctly.
        if not re.search(rf"\b{re.escape(name)}\b", question, flags=re.IGNORECASE):
            continue
        if _question_has_explicit_gender_for_name(question, name, gender):
            continue
        issues.append(
            make_issue(
                "Critical",
                "Blood Relation Ambiguity",
                "Explanation",
                evidence,
                "The explanation assumes gender/relation information that is not stated in the question. In blood-relation questions, do not conclude uncle/aunt/grandfather/grandmother from a generic sibling/parent/child relation unless the gender is given.",
                confidence="High",
            )
        )

    return issues
