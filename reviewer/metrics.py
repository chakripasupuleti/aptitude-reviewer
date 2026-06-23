"""Confusion-matrix metrics for the accuracy dashboard.

Ground truth comes from an optional "Has Issue" column in the input file.
Values accepted as "has issue": yes, y, true, 1 (case-insensitive).
Values accepted as "clean": no, n, false, 0, empty string.

Agent "flag"    = Rejected  OR  Needs Review
Agent "approve" = Approved  OR  Approved with Minor Fixes
"""
from __future__ import annotations
from typing import Any, Dict, List


_FLAG_STATUSES = {"rejected", "needs review"}
_APPROVE_STATUSES = {"approved", "approved with minor fixes"}

_FALSY  = {"no", "n", "false", "0", "", "nan", "none", "-", "valid"}


def _is_problem(ground_truth_value: Any) -> bool:
    """Return True if the GT value signals a problem.

    Handles two column styles:
      - Boolean-style: "Yes" / "No"
      - Seeded-issue-style: descriptive text (non-empty) means seeded problem,
        empty / "No" means clean question.
    """
    s = str(ground_truth_value).strip().lower()
    if s in _FALSY:
        return False
    # Any non-empty, non-falsy string (including issue descriptions) → has issue
    return bool(s)


def _is_flag(agent_status: str) -> bool:
    return agent_status.strip().lower() in _FLAG_STATUSES


def _metrics_from_counts(
    tp: int, fp: int, fn: int, tn: int, total: int, has_gt: bool,
    flagged_for_revision: int = 0, correctly_rejected: int = 0,
) -> Dict:
    """Build the standard metrics dict from already-classified TP/FP/FN/TN counts."""
    total_problems = tp + fn
    total_clean = fp + tn
    gt_total = tp + fp + fn + tn  # only GT-labelled rows — correct denominator for accuracy

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and (precision + recall) > 0
          else None)
    accuracy = (tp + tn) / gt_total if gt_total > 0 else None

    def _pct(v):
        return round(v * 100, 1) if v is not None else None

    insights = _build_insights(tp, fp, fn, precision, recall)

    return {
        "has_ground_truth": has_gt,
        "total": total,
        "total_problems": total_problems,
        "total_clean": total_clean,
        "TP": tp,
        "FP": fp,
        "FN": fn,
        "TN": tn,
        "flagged_for_revision": flagged_for_revision,
        "correctly_rejected": correctly_rejected,
        "correct_approvals": tn,
        "missed_issues": fn,
        "false_flags": fp,
        "precision": _pct(precision),
        "recall": _pct(recall),
        "f1": round(2 * (precision or 0) * (recall or 0) / ((precision or 0) + (recall or 0)), 2)
               if precision and recall else None,
        "accuracy": _pct(accuracy),
        "insights": insights,
    }


def compute_confusion_metrics(rows: List[Dict]) -> Dict:
    """Compute TP/FP/FN/TN and derived metrics from reviewed rows.

    Each row dict must have:
      - "agent_status": string
      - "ground_truth": any value interpretable as truthy/falsy (or None/missing)

    Returns a dict with:
      has_ground_truth, TP, FP, FN, TN,
      precision, recall, f1, accuracy,
      flagged_for_revision (NR true positives),
      correctly_rejected (Rejected true positives),
      correct_approvals (TN), missed_issues (FN), false_flags (FP),
      total, total_problems, total_clean
    """
    has_gt = any(r.get("ground_truth") is not None for r in rows)

    tp = fp = fn = tn = 0
    flagged_for_revision = 0  # TP subset where status = Needs Review
    correctly_rejected = 0    # TP subset where status = Rejected

    for r in rows:
        status = r.get("agent_status", "")
        flag = _is_flag(status)
        gt = r.get("ground_truth")

        if not has_gt or gt is None:
            continue

        problem = _is_problem(gt)
        if problem and flag:
            tp += 1
            if status.strip().lower() == "needs review":
                flagged_for_revision += 1
            else:
                correctly_rejected += 1
        elif problem and not flag:
            fn += 1
        elif not problem and flag:
            fp += 1
        else:
            tn += 1

    return _metrics_from_counts(tp, fp, fn, tn, len(rows), has_gt, flagged_for_revision, correctly_rejected)


def compute_feedback_confusion_metrics(feedback_records: List[Dict]) -> Dict:
    """Compute TP/FP/FN/TN from human Approve/Reject feedback on agent verdicts.

    The four cases, stated plainly:
      - agent says Approved, reviewer clicks Approve  -> correct                  (TN)
      - agent flags it (shows remarks), reviewer clicks Approve -> agent was right (TP)
      - agent says Approved, reviewer clicks Reject    -> agent is wrong, missed it (FN)
      - agent flags it, reviewer clicks Reject          -> agent is wrong, false flag (FP)

    Each feedback record (from the "Feedback" Google Sheet) has:
      - "Job ID", "Question No": identify the reviewed question
      - "Agent Status": what the agent decided (Rejected/Needs Review/Approved/...)
      - "User Verdict": "approve" (agent's call was correct) or "reject" (it was wrong)

    A question updated more than once is deduped to its latest submission
    (records are assumed to be in chronological append order, as gspread returns them).

    Mapping: agent flagged + approve -> TP, agent flagged + reject -> FP,
             agent approved + approve -> TN, agent approved + reject -> FN.
    """
    latest: Dict[tuple, Dict] = {}
    for rec in feedback_records:
        key = (rec.get("Job ID"), rec.get("Question No"))
        latest[key] = rec

    tp = fp = fn = tn = 0
    flagged_for_revision = 0
    correctly_rejected = 0

    for rec in latest.values():
        status = str(rec.get("Agent Status", ""))
        verdict = str(rec.get("User Verdict", "")).strip().lower()
        if verdict not in {"approve", "reject"}:
            continue

        flagged = _is_flag(status)
        agrees = verdict == "approve"

        if flagged and agrees:
            tp += 1
            if status.strip().lower() == "needs review":
                flagged_for_revision += 1
            else:
                correctly_rejected += 1
        elif flagged and not agrees:
            fp += 1
        elif not flagged and agrees:
            tn += 1
        else:
            fn += 1

    return _metrics_from_counts(
        tp, fp, fn, tn, len(latest), len(latest) > 0, flagged_for_revision, correctly_rejected
    )


def _build_insights(tp: int, fp: int, fn: int, precision, recall) -> List[Dict]:
    insights = []

    if precision is not None and precision >= 99.9:
        insights.append({
            "level": "success",
            "title": "Perfect precision",
            "body": "Every flag the agent raises is valid. No reviewer time wasted on false alarms.",
        })
    elif precision is not None and precision < 70:
        insights.append({
            "level": "warning",
            "title": f"Low precision ({precision:.1f}%)",
            "body": f"{fp} clean questions were incorrectly flagged. The agent is over-sensitive.",
        })

    if recall is not None and recall < 50:
        insights.append({
            "level": "warning",
            "title": f"Low recall ({recall:.1f}%)",
            "body": f"{fn} questions with fixable issues slipped through as approved. "
                    "The agent is conservative, not permissive.",
        })

    if recall is not None and recall < 50 and fp == 0:
        insights.append({
            "level": "info",
            "title": "Flagging threshold too high",
            "body": "Minor issues appear to be below the agent's detection bar. "
                    "Lowering the threshold would improve recall with minimal precision cost "
                    "(baseline FP is 0).",
        })

    if recall is not None and recall >= 80 and precision is not None and precision >= 80:
        insights.append({
            "level": "success",
            "title": "Good overall performance",
            "body": f"The agent correctly handles the majority of questions "
                    f"(recall {recall:.1f}%, precision {precision:.1f}%).",
        })

    return insights
