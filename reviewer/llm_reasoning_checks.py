import json
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Any, Optional

from .arithmetic_checks import evaluate_expression
from .llm_client import LLMClientError, MistralClientError, chat_completion, get_requested_model_name, normalize_model_name
from .llm_json import extract_json_array, extract_json_object
from .llm_prompts import SYSTEM_PROMPT, build_batch_user_prompt, build_user_prompt
from .models import ReviewIssue, make_issue


# Tool the LLM can call to verify a numeric calculation.
# Prevents false-flagging correct math due to the LLM's own arithmetic uncertainty.
VERIFY_CALC_TOOL = {
    "type": "function",
    "function": {
        "name": "verify_calculation",
        "description": (
            "Evaluate a mathematical expression and return its exact numeric result. "
            "Call this when you are unsure whether a calculation in the explanation is "
            "numerically correct BEFORE deciding to flag it. "
            "Use only +, -, *, /, (, ) in the expression — no words or variables."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "A pure arithmetic expression, e.g. '(150-100)/150*100'",
                }
            },
            "required": ["expression"],
        },
    },
}

LLM_TOOLS = [VERIFY_CALC_TOOL]

# Maximum tool-call turns before we stop the loop and return current state.
_MAX_TOOL_TURNS = 4


def _execute_tool_call(tool_name: str, tool_args: dict) -> str:
    """Execute a tool call requested by the LLM and return a string result."""
    if tool_name == "verify_calculation":
        expr = tool_args.get("expression", "")
        result = evaluate_expression(expr)
        if result is None:
            return f"Error: could not evaluate expression: {expr!r}"
        return str(float(result))
    return f"Error: unknown tool '{tool_name}'"

ALLOWED_SEVERITIES = {"Critical", "Major", "Minor", "Suggestion"}
ALLOWED_ERROR_TYPES = {
    "Formula Error",
    "Missing Formula",
    "Wrong Substitution",
    "Ambiguity",
    "Question-Explanation Mismatch",
    "Invalid Assumption",
    "Final Conclusion Mismatch",
    "Clarity Issue",
    "Needs Human Review",
}
ALLOWED_FIELDS = {"Question", "Explanation", "Options", "Key", "Multiple"}
ALLOWED_CONFIDENCE = {"High", "Medium", "Low"}

WEAK_WORDS = ("may be", "might", "possibly", "seems", "could be", "unclear")
DISALLOWED_PATTERNS = (
    "extra space",
    "extra spaces",
    "double space",
    "multiple spaces",
)

ROW_FIELDS = [
    "S. No",
    "Question",
    "Option A",
    "Option B",
    "Option C",
    "Option D",
    "Explanation",
    "Key",
]


@dataclass(frozen=True)
class LLMReviewResult:
    issues: List[ReviewIssue]
    status: str
    error: str = ""
    model: str = ""
    # Evidence strings of V1 "Data Mismatch" findings that V2 identified as
    # false positives (e.g. a sentence adverb mistaken for a person's name).
    v1_false_positive_evidence: List[str] = field(default_factory=list)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except Exception:
        pass
    return str(value)


def row_to_llm_payload(row: Mapping[str, Any]) -> dict:
    return {field: _stringify(row.get(field, "")) for field in ROW_FIELDS}


def row_to_llm_text(row: Mapping[str, Any]) -> str:
    lines = [f"{field}: {_stringify(row.get(field, ''))}" for field in ROW_FIELDS]
    return "\n".join(lines)


def issues_to_llm_payload(issues: Iterable[ReviewIssue]) -> list[dict]:
    return [
        {
            "severity": issue.severity,
            "error_type": issue.error_type,
            "field": issue.field,
            "evidence": issue.evidence,
            "reason": issue.reason,
            "suggested_fix": issue.suggested_fix,
            "confidence": issue.confidence,
        }
        for issue in issues
    ]


def _contains_disallowed_feedback(issue: dict) -> bool:
    text = " ".join(str(issue.get(k, "")) for k in ["error_type", "evidence", "reason", "suggested_fix"]).lower()
    return any(pattern in text for pattern in DISALLOWED_PATTERNS)



def _looks_complex_reasoning_text(text: str) -> bool:
    lower = text.lower()
    return any(
        term in lower
        for term in [
            "rank", "ranking", "position", "from the left", "from the right",
            "clock", "hour hand", "minute hand", "direction", "seating",
            "blood relation", "relation", "interchange", "shifted", "queue",
        ]
    )


def _is_grammar_only_v1(v1_issues: Iterable[ReviewIssue]) -> bool:
    issues = list(v1_issues)
    return bool(issues) and all(issue.error_type == "Grammar / Formation Error" for issue in issues)


def _postprocess_v2_issues(
    row: Mapping[str, Any],
    issues: List[ReviewIssue],
    v1_issues: Iterable[ReviewIssue],
) -> List[ReviewIssue]:
    """Precision cleanup to reduce false positives from the LLM layer."""
    v1_list = list(v1_issues)
    row_text = " ".join(_stringify(row.get(field, "")) for field in ROW_FIELDS)
    complex_row = _looks_complex_reasoning_text(row_text)
    grammar_only = _is_grammar_only_v1(v1_list)

    cleaned: List[ReviewIssue] = []
    for issue in issues:
        # If V1 found only grammar/formation issues, do not allow V2 to escalate
        # to formula/substitution/final mismatch unless the LLM evidence is a
        # direct contradiction rather than a different solve.
        if grammar_only and issue.error_type in {"Formula Error", "Wrong Substitution", "Final Conclusion Mismatch"}:
            continue

        suggested_fix = issue.suggested_fix
        severity = issue.severity
        error_type = issue.error_type
        confidence = issue.confidence

        # For complex reasoning topics, a suggested corrected answer is often the
        # riskiest part. Keep the issue, but remove the correction unless the
        # model is highly confident.
        if complex_row and confidence != "High":
            suggested_fix = ""
            error_type = "Needs Human Review"
            severity = "Major" if severity == "Critical" else severity
            confidence = "Low"
        elif complex_row and issue.error_type in {"Formula Error", "Wrong Substitution", "Final Conclusion Mismatch"}:
            # Even with high confidence, avoid returning speculative alternate
            # answers for complex topics; reviewer should flag the issue only.
            suggested_fix = ""

        cleaned.append(
            make_issue(
                severity=severity,
                error_type=error_type,
                field=issue.field,
                evidence=issue.evidence,
                reason=issue.reason,
                suggested_fix=suggested_fix,
                confidence=confidence,
            )
        )

    # Keep the primary/root issue and at most one independent secondary issue.
    # Prefer higher severity, but avoid a long noisy list.
    return cleaned[:2]

def _normalize_issue(raw: dict) -> Optional[ReviewIssue]:
    severity = str(raw.get("severity", "")).strip()
    error_type = str(raw.get("error_type", "")).strip()
    field = str(raw.get("field", "")).strip()
    evidence = str(raw.get("evidence", "")).strip()
    reason = str(raw.get("reason", "")).strip()
    suggested_fix = str(raw.get("suggested_fix", "")).strip()
    confidence = str(raw.get("confidence", "")).strip()

    if severity not in ALLOWED_SEVERITIES:
        severity = "Major"
    if error_type not in ALLOWED_ERROR_TYPES:
        error_type = "Needs Human Review"
    if field not in ALLOWED_FIELDS:
        field = "Multiple"
    if confidence not in ALLOWED_CONFIDENCE:
        confidence = "Low"

    if not evidence or not reason:
        return None
    if _contains_disallowed_feedback(raw):
        return None

    weak_text = f"{evidence} {reason} {suggested_fix}".lower()
    if confidence == "Low" or any(word in weak_text for word in WEAK_WORDS):
        error_type = "Needs Human Review"
        confidence = "Low"
        if severity == "Critical":
            severity = "Major"

    return make_issue(
        severity=severity,
        error_type=error_type,
        field=field,
        evidence=evidence,
        reason=reason,
        suggested_fix=suggested_fix,
        confidence=confidence,
    )


def _extract_v1_false_positives(raw: Any, v1_issues: Iterable[ReviewIssue]) -> List[str]:
    """Return evidence strings V2 flagged as false positives, restricted to V1
    "Data Mismatch" findings whose evidence exactly matches (scope guard so V2
    can only veto this one fragile check, never other V1 categories)."""
    if not isinstance(raw, list):
        return []
    data_mismatch_evidence = {
        issue.evidence.strip().lower() for issue in v1_issues if issue.error_type == "Data Mismatch"
    }
    vetoed: List[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        evidence = str(entry.get("evidence", "")).strip()
        if evidence and evidence.lower() in data_mismatch_evidence:
            vetoed.append(evidence)
    return vetoed


def _dedupe_against_v1(v2_issues: List[ReviewIssue], v1_issues: Iterable[ReviewIssue]) -> List[ReviewIssue]:
    v1_text = "\n".join(
        f"{issue.error_type} {issue.field} {issue.evidence} {issue.reason}".lower() for issue in v1_issues
    )
    result: List[ReviewIssue] = []
    for issue in v2_issues:
        compact = f"{issue.error_type} {issue.field} {issue.evidence}".lower()
        # Avoid exact echo of deterministic issue, but allow a reasoning issue with different evidence.
        if compact and compact in v1_text:
            continue
        result.append(issue)
    return result


def run_llm_reasoning(
    row: Mapping[str, Any],
    v1_issues: Iterable[ReviewIssue],
    model: Optional[str] = None,
) -> LLMReviewResult:
    resolved_model = normalize_model_name(model) if model else ""
    v1_list = list(v1_issues)
    row_text = row_to_llm_text(row)
    v1_findings_json = json.dumps(issues_to_llm_payload(v1_list), ensure_ascii=False)
    user_prompt = build_user_prompt(row_text=row_text, v1_findings_json=v1_findings_json)

    messages: List[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    content: Optional[str] = None
    try:
        for _ in range(_MAX_TOOL_TURNS + 1):
            response = chat_completion(
                messages=messages,
                model=model,
                temperature=0.0,
                tools=LLM_TOOLS,
            )
            if isinstance(response, dict):
                # LLM requested tool calls — execute them and feed results back.
                tool_calls = response.get("message", {}).get("tool_calls", [])
                if not tool_calls:
                    break
                messages.append(response["message"])
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    try:
                        fn_args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                    except json.JSONDecodeError:
                        fn_args = {}
                    tool_result = _execute_tool_call(fn_name, fn_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_result,
                    })
            else:
                content = response
                break
    except (LLMClientError, MistralClientError) as exc:
        return LLMReviewResult(issues=[], status="Failed", error=str(exc), model=resolved_model)

    if content is None:
        return LLMReviewResult(
            issues=[],
            status="Failed",
            error="LLM did not produce a final text response after tool calls.",
            model=resolved_model,
        )

    parsed = extract_json_object(content)
    if not parsed or not isinstance(parsed.get("issues", []), list):
        return LLMReviewResult(
            issues=[],
            status="Invalid JSON",
            error="V2 reasoning check returned invalid JSON.",
            model=resolved_model,
        )

    normalized: List[ReviewIssue] = []
    for raw in parsed.get("issues", []):
        if not isinstance(raw, dict):
            continue
        issue = _normalize_issue(raw)
        if issue is not None:
            normalized.append(issue)

    deduped = _dedupe_against_v1(normalized, v1_list)
    cleaned = _postprocess_v2_issues(row=row, issues=deduped, v1_issues=v1_list)
    v1_false_positives = _extract_v1_false_positives(parsed.get("v1_false_positives", []), v1_list)

    return LLMReviewResult(
        issues=cleaned,
        status="Success",
        error="",
        model=resolved_model,
        v1_false_positive_evidence=v1_false_positives,
    )


def run_llm_reasoning_batch(
    rows_and_v1_issues: list,
    model: Optional[str] = None,
) -> List["LLMReviewResult"]:
    """Run LLM reasoning for a batch of rows in a single API call.

    rows_and_v1_issues: list of (row, v1_issues, sno) tuples
    Returns one LLMReviewResult per input row, in the same order.
    """
    n = len(rows_and_v1_issues)
    if n == 0:
        return []

    resolved_model = normalize_model_name(model) if model else get_requested_model_name()

    entries = [
        (sno, row_to_llm_text(row), json.dumps(issues_to_llm_payload(v1), ensure_ascii=False))
        for row, v1, sno in rows_and_v1_issues
    ]
    user_prompt = build_batch_user_prompt(entries)

    try:
        content = chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.0,
            max_tokens=12000,
        )
    except (LLMClientError, MistralClientError) as exc:
        return [
            LLMReviewResult(issues=[], status="Failed", error=str(exc), model=resolved_model)
            for _ in range(n)
        ]

    try:
        arr = extract_json_array(content)
    except ValueError as exc:
        return [
            LLMReviewResult(issues=[], status="Invalid JSON", error=str(exc), model=resolved_model)
            for _ in range(n)
        ]

    results: List[LLMReviewResult] = []
    for i, (row, v1_issues, _sno) in enumerate(rows_and_v1_issues):
        item = arr[i] if i < len(arr) else {}
        if not isinstance(item, dict):
            results.append(LLMReviewResult(issues=[], status="Invalid JSON", model=resolved_model))
            continue

        raw_issues = item.get("issues", [])
        if not isinstance(raw_issues, list):
            raw_issues = []

        normalized: List[ReviewIssue] = []
        for iss in raw_issues:
            if not isinstance(iss, dict):
                continue
            issue = _normalize_issue(iss)
            if issue is not None:
                normalized.append(issue)

        deduped = _dedupe_against_v1(normalized, v1_issues)
        cleaned = _postprocess_v2_issues(row=row, issues=deduped, v1_issues=v1_issues)
        v1_false_positives = _extract_v1_false_positives(item.get("v1_false_positives", []), v1_issues)
        results.append(LLMReviewResult(
            issues=cleaned, status="Success", error="", model=resolved_model,
            v1_false_positive_evidence=v1_false_positives,
        ))

    return results


def check_llm_reasoning(
    row: Mapping[str, Any],
    v1_issues: Iterable[ReviewIssue],
    model: Optional[str] = None,
) -> List[ReviewIssue]:
    """Backward-compatible wrapper: returns only content-review issues."""
    return run_llm_reasoning(row=row, v1_issues=v1_issues, model=model).issues
