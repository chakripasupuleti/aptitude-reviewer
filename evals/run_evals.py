#!/usr/bin/env python3
"""
Accuracy eval runner for the aptitude reviewer.

Reads evals_1.xlsx and evals_2.xlsx, runs V1 checks (and optionally LLM) on each row,
compares agent output to the human-written Remarks column, and prints a precision/recall
report plus tables of false negatives and false positives.

Usage:
    python evals/run_evals.py
    python evals/run_evals.py --use-llm
    python evals/run_evals.py --output evals/results.xlsx
"""
import argparse
import io
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow imports from the project root regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Force UTF-8 output so rupee signs, math symbols, etc. don't crash on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pandas as pd

from app import _run_v1_checks, _build_result
from reviewer.llm_reasoning_checks import run_llm_reasoning
from reviewer.normalize import normalize_columns, normalize_input_values, cell_text

EVAL_DIR = Path(__file__).parent
EVAL_FILES = [EVAL_DIR / "evals_1.xlsx", EVAL_DIR / "evals_2.xlsx"]

# ---------------------------------------------------------------------------
# Remarks → expected category classifier
# ---------------------------------------------------------------------------

# Ordered by specificity — first match wins.
_REMARK_KEYWORDS: List[Tuple[str, str]] = [
    ("key is not there", "Answer Not in Options"),
    ("key is not in",    "Answer Not in Options"),
    ("not there in options", "Answer Not in Options"),
    ("not in options",   "Answer Not in Options"),
    ("key mismatch",     "Key Mismatch"),
    ("wrong key",        "Key Mismatch"),
    ("calculation error", "Calculation Error"),
    ("data mismatch",    "Data Mismatch"),
    ("latex error",      "LaTeX"),
    ("values swapped",   "Wrong Substitution"),
    ("value swapped",    "Wrong Substitution"),
    ("gender mistake",   "Pronoun/Gender Error"),
    ("pronoun",          "Pronoun/Gender Error"),
    ("missing field",    "Missing Field"),
    ("missing key",      "Missing Field"),
]

# Maps an agent error_type to the expected categories it satisfies.
_SATISFIES: Dict[str, List[str]] = {
    "Key Mismatch":               ["Key Mismatch"],
    "Answer Not in Options":      ["Answer Not in Options"],
    "Calculation Error":          ["Calculation Error"],
    "Percentage Calculation Error": ["Calculation Error"],
    "Unit Conversion Error":      ["Calculation Error"],
    "Data Mismatch":              ["Data Mismatch"],
    "LaTeX Formatting Error":     ["LaTeX"],
    "Wrong Substitution":         ["Wrong Substitution"],
    "Pronoun/Gender Error":       ["Pronoun/Gender Error"],
    "Missing Field":              ["Missing Field"],
    "Missing Explanation":        ["Missing Field"],
    "Currency Symbol Error":      ["LaTeX"],
    "LaTeX Formatting":           ["LaTeX"],
    "Blood Relation Ambiguity":   ["Pronoun/Gender Error"],
}


def _classify_remarks(remarks: str) -> Tuple[bool, str]:
    """Return (is_clean, category). is_clean=True means the row should have no issues."""
    if not isinstance(remarks, str) or not remarks.strip():
        return True, "Valid"
    r = remarks.strip().lower()
    if r == "valid":
        return True, "Valid"
    for keyword, cat in _REMARK_KEYWORDS:
        if keyword in r:
            return False, cat
    return False, "Other Issue"


def _category_matched(issues, expected_cat: str) -> bool:
    """Return True if any agent issue satisfies the expected category."""
    if expected_cat in ("Valid", "Other Issue"):
        return False  # not applicable
    for issue in issues:
        if expected_cat in _SATISFIES.get(issue.error_type, []):
            return True
    return False


# ---------------------------------------------------------------------------
# Per-row evaluation
# ---------------------------------------------------------------------------

def _eval_row(row, use_llm: bool, model: Optional[str]) -> Tuple[list, dict]:
    v1_issues = _run_v1_checks(row)
    llm_result = None
    if use_llm:
        llm_result = run_llm_reasoning(row, v1_issues=list(v1_issues), model=model)
    result = _build_result(row, v1_issues, llm_result)
    all_issues = list(v1_issues) + (list(llm_result.issues) if llm_result else [])
    return all_issues, result


def run_eval_file(filepath: Path, use_llm: bool, model: Optional[str] = None) -> List[Dict[str, Any]]:
    df = pd.read_excel(filepath)
    df = normalize_columns(df)
    df = normalize_input_values(df)

    rows_out = []
    for idx, row in df.iterrows():
        sno = str(cell_text(row, "S. No") or (idx + 1))
        remarks = str(row.get("Remarks", "") or "")
        is_clean, expected_cat = _classify_remarks(remarks)

        issues, _result = _eval_row(row, use_llm=use_llm, model=model)
        agent_flagged = len(issues) > 0

        if is_clean and not agent_flagged:
            outcome = "TN"
        elif is_clean and agent_flagged:
            outcome = "FP"
        elif not is_clean and agent_flagged:
            outcome = "TP"
        else:
            outcome = "FN"

        cat_match: Optional[bool] = None
        if outcome == "TP":
            cat_match = _category_matched(issues, expected_cat)

        rows_out.append({
            "File": filepath.name,
            "SNO": sno,
            "IsClean": is_clean,
            "ExpectedCat": expected_cat,
            "Expected": "CLEAN" if is_clean else f"FLAGGED ({expected_cat})",
            "Agent": "CLEAN" if not agent_flagged else "FLAGGED",
            "Outcome": outcome,
            "CatMatch": cat_match,
            "AgentIssues": "; ".join(f"{i.error_type}" for i in issues),
            "Remarks": remarks,
            "Question": (cell_text(row, "Question") or "")[:80],
        })
    return rows_out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _metrics(rows: List[Dict]) -> Dict[str, Any]:
    tp = sum(1 for r in rows if r["Outcome"] == "TP")
    tn = sum(1 for r in rows if r["Outcome"] == "TN")
    fp = sum(1 for r in rows if r["Outcome"] == "FP")
    fn = sum(1 for r in rows if r["Outcome"] == "FN")
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {"TP": tp, "TN": tn, "FP": fp, "FN": fn,
            "Precision": round(precision, 3),
            "Recall": round(recall, 3),
            "F1": round(f1, 3)}


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

_SEP = "=" * 72

def _print_file_section(fname: str, rows: List[Dict]) -> None:
    m = _metrics(rows)
    print(f"\n{_SEP}")
    print(f"FILE: {fname}  ({len(rows)} rows)")
    print(_SEP)
    hdr = f"{'SNO':<5}  {'Expected':<30}  {'Agent':<8}  {'OK':<4}  {'Cat':<4}  {'Agent Issues'}"
    print(hdr)
    print("-" * 72)
    for r in rows:
        ok  = "OK" if r["Outcome"] in ("TP", "TN") else "!!"
        cat = ("Y" if r["CatMatch"] else "N") if r["CatMatch"] is not None else "-"
        print(f"{r['SNO']:<5}  {r['Expected']:<30}  {r['Agent']:<8}  {ok:<4}  {cat:<4}  {r['AgentIssues']}")
    print(f"\n  TP={m['TP']}  TN={m['TN']}  FP={m['FP']}  FN={m['FN']}"
          f"  |  Precision={m['Precision']:.2f}  Recall={m['Recall']:.2f}  F1={m['F1']:.2f}")


def print_report(all_rows: List[Dict]) -> None:
    # Per-file sections
    files: Dict[str, List[Dict]] = {}
    for r in all_rows:
        files.setdefault(r["File"], []).append(r)
    for fname, rows in files.items():
        _print_file_section(fname, rows)

    # Combined summary
    m = _metrics(all_rows)
    print(f"\n{_SEP}")
    print(f"COMBINED  ({len(all_rows)} rows total)")
    print(f"  TP={m['TP']}  TN={m['TN']}  FP={m['FP']}  FN={m['FN']}"
          f"  |  Precision={m['Precision']:.2f}  Recall={m['Recall']:.2f}  F1={m['F1']:.2f}")
    print(_SEP)

    # False Negatives — agent missed a real issue
    fn_rows = [r for r in all_rows if r["Outcome"] == "FN"]
    if fn_rows:
        print(f"\n{'-'*72}")
        print(f"FALSE NEGATIVES ({len(fn_rows)})  -- agent missed real issues")
        print(f"{'-'*72}")
        for r in fn_rows:
            print(f"\n  SNO {r['SNO']}  ({r['File']})")
            print(f"  Expected : {r['Expected']}")
            print(f"  Remarks  : {r['Remarks']}")
            print(f"  Question : {r['Question']}")
    else:
        print("\nFALSE NEGATIVES: none")

    # False Positives — agent flagged a valid row
    fp_rows = [r for r in all_rows if r["Outcome"] == "FP"]
    if fp_rows:
        print(f"\n{'-'*72}")
        print(f"FALSE POSITIVES ({len(fp_rows)})  -- agent wrongly flagged a valid row")
        print(f"{'-'*72}")
        for r in fp_rows:
            print(f"\n  SNO {r['SNO']}  ({r['File']})")
            print(f"  Agent issues : {r['AgentIssues']}")
            print(f"  Question     : {r['Question']}")
    else:
        print("\nFALSE POSITIVES: none")

    # Category-match breakdown for TPs
    tp_rows = [r for r in all_rows if r["Outcome"] == "TP"]
    if tp_rows:
        cat_hit  = sum(1 for r in tp_rows if r["CatMatch"] is True)
        cat_miss = sum(1 for r in tp_rows if r["CatMatch"] is False)
        print(f"\nCategory accuracy on TPs: {cat_hit}/{len(tp_rows)} correct category"
              f"  ({cat_miss} caught wrong type)")


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def export_excel(all_rows: List[Dict], path: str) -> None:
    export_cols = ["File", "SNO", "Expected", "Agent", "Outcome", "CatMatch",
                   "AgentIssues", "Remarks", "Question"]
    df = pd.DataFrame(all_rows)[export_cols]
    df.to_excel(path, index=False)
    print(f"\nResults written to: {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate reviewer accuracy against ground-truth eval files."
    )
    parser.add_argument("--use-llm", action="store_true",
                        help="Also run LLM reasoning checks (costs API calls).")
    parser.add_argument("--llm-model", default=None,
                        help="Override LLM model (e.g. anthropic/claude-sonnet-4-6).")
    parser.add_argument("--output", default=None,
                        help="Write full result table to this Excel file.")
    args = parser.parse_args()

    all_rows: List[Dict] = []
    for fpath in EVAL_FILES:
        if not fpath.exists():
            print(f"WARNING: {fpath} not found — skipping.")
            continue
        print(f"Processing {fpath.name} ...", end=" ", flush=True)
        rows = run_eval_file(fpath, use_llm=args.use_llm, model=args.llm_model)
        print(f"{len(rows)} rows.")
        all_rows.extend(rows)

    if not all_rows:
        print("No eval files found. Place evals_1.xlsx and evals_2.xlsx in the evals/ directory.")
        return 1

    print_report(all_rows)

    if args.output:
        export_excel(all_rows, args.output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
