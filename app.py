import argparse
import os
import sys
import time
from typing import List

import pandas as pd

from reviewer.arithmetic_checks import check_arithmetic
from reviewer.consolidator import consolidate
from reviewer.constants import REQUIRED_COLUMNS, REVIEW_COLUMNS
from reviewer.currency_checks import check_currency_symbol
from reviewer.data_mismatch_checks import check_data_mismatch
from reviewer.export import export_reviewed
from reviewer.field_checks import check_required_fields
from reviewer.final_answer_checks import check_final_answer_and_key
from reviewer.gender_relation_checks import check_gender_relation
from reviewer.grammar_checks import check_grammar_basics, check_verification_step
from reviewer.ingest import read_input
from reviewer.issue_filters import filter_known_false_positives
from reviewer.key_checks import check_key_format
from reviewer.latex_checks import check_latex_formatting
from reviewer.llm_client import get_requested_model_name
from reviewer.llm_reasoning_checks import LLMReviewResult, run_llm_reasoning, run_llm_reasoning_batch
from reviewer.models import ReviewIssue
from reviewer.normalize import missing_required_columns, normalize_columns, normalize_input_values
from reviewer.topic_checks import check_topic_specific
from reviewer.option_equivalence import check_duplicate_options


def _run_v1_checks(row) -> List[ReviewIssue]:
    """Run all deterministic V1 checks and return the raw issues list."""
    issues: List[ReviewIssue] = []
    issues.extend(check_required_fields(row))
    issues.extend(check_key_format(row))
    issues.extend(check_duplicate_options(row))
    issues.extend(check_currency_symbol(row))
    issues.extend(check_latex_formatting(row))
    arithmetic_issues = check_arithmetic(row)
    issues.extend(arithmetic_issues)
    issues.extend(check_final_answer_and_key(row, arithmetic_issues=arithmetic_issues))
    issues.extend(check_data_mismatch(row))
    issues.extend(check_topic_specific(row))
    issues.extend(check_grammar_basics(row))
    issues.extend(check_verification_step(row))
    issues.extend(check_gender_relation(row))
    return issues


def _build_result(row, v1_issues: List[ReviewIssue], llm_result: "LLMReviewResult | None" = None) -> dict:
    """Combine V1 issues + optional LLM result into the final review dict."""
    all_issues = list(v1_issues)
    llm_status = "Not Run"
    llm_error = ""
    llm_model_used = ""

    if llm_result is not None:
        # V2 may veto specific V1 "Data Mismatch" findings it judged to be false
        # positives (e.g. a sentence adverb mistaken for a person's name). Scoped
        # narrowly by _extract_v1_false_positives; only applies on LLM success.
        if llm_result.status == "Success" and llm_result.v1_false_positive_evidence:
            vetoed = {e.strip().lower() for e in llm_result.v1_false_positive_evidence}
            all_issues = [
                i for i in all_issues
                if not (i.error_type == "Data Mismatch" and i.evidence.strip().lower() in vetoed)
            ]
        all_issues.extend(llm_result.issues)
        llm_status = llm_result.status
        llm_error = llm_result.error
        llm_model_used = llm_result.model or get_requested_model_name()

    all_issues = filter_known_false_positives(row, all_issues)
    result = consolidate(all_issues)

    used_llm = llm_result is not None
    if used_llm and llm_status not in {"Success", "Not Run"}:
        if result.get("Agent Status") == "Approved":
            result["Agent Status"] = "System Error / Needs Retry"
            result["Confidence"] = "Low"
        elif result.get("Agent Status") == "Approved with Minor Fixes":
            result["Agent Status"] = "Needs Review"
            result["Confidence"] = "Low"

    result["LLM Status"] = llm_status
    result["LLM Error"] = llm_error
    result["LLM Model"] = llm_model_used
    return result


def review_row(row, use_llm: bool = False, llm_model: str | None = None) -> dict:
    v1_issues = _run_v1_checks(row)
    llm_result = None
    if use_llm:
        llm_result = run_llm_reasoning(row, v1_issues=list(v1_issues), model=llm_model)
    return _build_result(row, v1_issues, llm_result)


def _flush_batch(
    batch: list,
    results_map: dict,
    model: str | None,
    delay_seconds: float,
) -> None:
    """Call run_llm_reasoning_batch for a batch and store results by index.

    batch: list of (idx, row, v1_issues) tuples
    run_llm_reasoning_batch expects (row, v1_issues, sno) tuples.
    """
    api_batch = [(row, v1, str(row.get("S. No", idx))) for (idx, row, v1) in batch]
    llm_results = run_llm_reasoning_batch(api_batch, model=model)
    for (idx, _row, _v1), llm_result in zip(batch, llm_results):
        results_map[idx] = llm_result
    if delay_seconds > 0:
        time.sleep(delay_seconds)


def review_file(
    input_path: str,
    output_path: str,
    use_llm: bool = False,
    llm_model: str | None = None,
    llm_delay_seconds: float | None = None,
    batch_size: int = 5,
    skip_llm_if_critical: bool = False,
) -> pd.DataFrame:
    df = read_input(input_path)
    df = normalize_columns(df)
    df = normalize_input_values(df)

    missing = missing_required_columns(df)
    if missing:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing) + ". Required columns are: " + ", ".join(REQUIRED_COLUMNS)
        )

    if llm_delay_seconds is None:
        llm_delay_seconds = float(os.getenv("OPENROUTER_ROW_DELAY_SECONDS", "0.5") or 0)

    # Pass 1: run all V1 checks row-by-row (fast, in-process)
    v1_pairs: List[tuple] = []
    for _, row in df.iterrows():
        v1_pairs.append((row, _run_v1_checks(row)))

    # Pass 2: batch LLM calls
    llm_results_map: dict = {}
    if use_llm:
        batch: list = []
        for idx, (row, v1_issues) in enumerate(v1_pairs):
            if skip_llm_if_critical and any(i.severity == "Critical" for i in v1_issues):
                continue
            batch.append((idx, row, v1_issues))
            if len(batch) >= batch_size:
                _flush_batch(batch, llm_results_map, llm_model, llm_delay_seconds)
                batch = []
        if batch:
            _flush_batch(batch, llm_results_map, llm_model, llm_delay_seconds)

    # Combine V1 + LLM results
    reviewed_records = []
    for idx, (row, v1_issues) in enumerate(v1_pairs):
        llm_result = llm_results_map.get(idx)
        reviewed_records.append(_build_result(row, v1_issues, llm_result))

    review_df = pd.DataFrame(reviewed_records)
    for col in REVIEW_COLUMNS:
        if col not in review_df.columns:
            review_df[col] = ""

    output_df = pd.concat([df.reset_index(drop=True), review_df[REVIEW_COLUMNS].reset_index(drop=True)], axis=1)
    export_reviewed(output_df, output_path)
    return output_df


def main() -> int:
    parser = argparse.ArgumentParser(description="Aptitude Content Reviewer V1.2 with optional V2 LLM reasoning")
    parser.add_argument("input", help="Input .csv or .xlsx file")
    parser.add_argument("output", help="Output .csv or .xlsx file")
    parser.add_argument("--use-llm", action="store_true", help="Enable V2 LLM reasoning checks after deterministic V1 checks")
    parser.add_argument("--llm-model", default=None, help="Override model. Example: anthropic/claude-sonnet-4-6")
    parser.add_argument("--llm-delay", type=float, default=None, help="Seconds to wait between LLM batch calls. Default: env OPENROUTER_ROW_DELAY_SECONDS or 0.5")
    parser.add_argument("--batch-size", type=int, default=5, help="Rows per LLM batch call (default 5, max ~15). Ignored when --use-llm is not set.")
    parser.add_argument("--skip-llm-if-critical", action="store_true", help="Skip V2 LLM for rows where V1 already found a Critical issue (saves API calls).")
    args = parser.parse_args()

    try:
        if args.use_llm:
            print(f"Using LLM model: {get_requested_model_name(args.llm_model)} | batch-size={args.batch_size}")
        result = review_file(
            args.input, args.output,
            use_llm=args.use_llm,
            llm_model=args.llm_model,
            llm_delay_seconds=args.llm_delay,
            batch_size=args.batch_size,
            skip_llm_if_critical=args.skip_llm_if_critical,
        )
        print(f"Reviewed {len(result)} rows. Output written to: {args.output}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
