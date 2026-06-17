"""FastAPI backend for the Aptitude Content Reviewer web UI.

Run:  uvicorn server:app --reload --port 8000
Then open http://localhost:8000
"""
import os
import sys
import time
import uuid
import threading
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── allow importing reviewer package ──────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from app import _build_result, _run_v1_checks, review_row
from reviewer.llm_reasoning_checks import LLMReviewResult, run_llm_reasoning_batch
from reviewer.constants import REQUIRED_COLUMNS, REVIEW_COLUMNS
from reviewer.export import export_reviewed
from reviewer.ingest import read_input
from reviewer.metrics import compute_confusion_metrics
from reviewer.normalize import (
    cell_text,
    missing_required_columns,
    normalize_columns,
    normalize_input_values,
)
from reviewer.sheets_logger import fetch_error_history, fetch_run_history, log_feedback, log_run, sheets_configured

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="Aptitude Reviewer API", docs_url="/api/docs")

# ── In-memory job store (keyed by job_id) ─────────────────────────────────────
jobs: Dict[str, Any] = {}

# ── Rubric category → error type mapping ──────────────────────────────────────
RUBRIC_MAP: Dict[str, set] = {
    "Calculation": {
        "Calculation Error",
        "Percentage Calculation Error",
        "Unit Conversion Error",
        "Wrong Reasoning",
    },
    "Key Mismatch": {"Key Mismatch", "Explanation-Key Conflict"},
    "Key Missing": {"Answer Not in Options", "Multiple Correct Options"},
    "Data Mismatch": {"Data Mismatch", "Direction Mismatch"},
    "Entity / Value Swap": {
        "Wrong Substitution",
        "Question-Explanation Mismatch",
        "Formula Error",
        "Missing Formula",
    },
    "Conclusion Mismatch": {"Final Conclusion Mismatch"},
    "LaTeX Formatting": {"LaTeX Formatting"},
    "Pronoun / Gender": {
        "Blood Relation Ambiguity",
        "Pronoun/Gender Error",
        "Invalid Assumption",
        "Ambiguity",
    },
    "Name Consistency": {"Name Mismatch"},
    "Question Error": {
        "Grammar / Formation Error",
        "Clarity Issue",
        "Question Wording / Label Mismatch",
        "Missing Field",
        "Invalid Key Format",
    },
    "Duplicate": {"Duplicate Options"},
}

# Optional ground-truth column names accepted in the input file
_GT_COLUMNS = {"Has Issue", "has issue", "Has_Issue", "Ground Truth", "Seeded Issue", "Seeded V2 Issue"}


def _compute_rubric(error_types_str: str) -> Dict[str, str]:
    types = {e.strip() for e in error_types_str.split(";") if e.strip()}
    return {cat: ("Error" if types & members else "Pass") for cat, members in RUBRIC_MAP.items()}


def _sno_label(idx: int) -> str:
    return f"Q{idx + 1:03d}"


def _sno_display(value) -> str:
    s = str(value).strip() if value is not None else ""
    return s if s else ""


def _detect_gt_column(df: pd.DataFrame) -> Optional[str]:
    """Return the ground-truth column name if present, else None."""
    for col in df.columns:
        if col.strip() in _GT_COLUMNS:
            return col
    return None


def _gt_value(row, gt_col: Optional[str]) -> Optional[str]:
    if gt_col is None:
        return None
    val = row.get(gt_col, None)
    if val is None or (isinstance(val, float) and val != val):
        return None
    return str(val).strip()


_BATCH_SIZE = 40

# ── Background review job ──────────────────────────────────────────────────────

def _build_row_result(label: str, row, result: dict, gt_col) -> dict:
    err_str = result.get("Error Types", "")
    return {
        "sno": label,
        "display_sno": _sno_display(row.get("S. No", "")) or label,
        "question": cell_text(row, "Question"),
        "option_a": cell_text(row, "Option A"),
        "option_b": cell_text(row, "Option B"),
        "option_c": cell_text(row, "Option C"),
        "option_d": cell_text(row, "Option D"),
        "key": cell_text(row, "Key"),
        "explanation": cell_text(row, "Explanation"),
        "agent_status": result.get("Agent Status", ""),
        "error_types": [e.strip() for e in err_str.split(";") if e.strip()],
        "remarks": result.get("Agent Remarks", ""),
        "suggested_corrections": result.get("Suggested Corrections", ""),
        "reviewed_key": result.get("Reviewed Key", ""),
        "confidence": result.get("Confidence", ""),
        "llm_status": result.get("LLM Status", "Not Run"),
        "llm_model": result.get("LLM Model", ""),
        "rubric": _compute_rubric(err_str),
        "ground_truth": _gt_value(row, gt_col),
    }


def _run_job(job_id: str, input_path: str, output_path: str, use_llm: bool) -> None:
    job = jobs[job_id]
    try:
        df = read_input(input_path)
        df = normalize_columns(df)
        df = normalize_input_values(df)

        missing = missing_required_columns(df)
        if missing:
            raise ValueError(f"Missing columns: {', '.join(missing)}")

        gt_col = _detect_gt_column(df)
        job["total"] = len(df)
        job["status"] = "running"
        job["has_ground_truth"] = gt_col is not None

        # ── Phase 1: deterministic V1 checks for every row ─────────────────────
        v1_data = []  # list of (label, row, v1_issues)
        for idx, (_, row) in enumerate(df.iterrows()):
            label = _sno_label(idx)
            job["current"] = label
            v1_issues = _run_v1_checks(row)
            v1_data.append((label, row, v1_issues))
            job["done"] = idx + 1

        # ── Phase 2: batch LLM in groups of _BATCH_SIZE ────────────────────────
        llm_results: list = []
        if use_llm:
            total_batches = (len(v1_data) + _BATCH_SIZE - 1) // _BATCH_SIZE
            for b_idx in range(0, len(v1_data), _BATCH_SIZE):
                batch = v1_data[b_idx : b_idx + _BATCH_SIZE]
                batch_num = b_idx // _BATCH_SIZE + 1
                job["current"] = f"LLM batch {batch_num}/{total_batches}"
                batch_inputs = [(row, v1, label) for label, row, v1 in batch]
                batch_results = run_llm_reasoning_batch(batch_inputs, model=None)
                llm_results.extend(batch_results)
        else:
            llm_results = [LLMReviewResult(issues=[], status="Not Run")] * len(v1_data)

        # ── Phase 3: assemble final results ────────────────────────────────────
        reviewed_records = []
        row_results = []
        for (label, row, v1_issues), llm_result in zip(v1_data, llm_results):
            result = _build_result(row, v1_issues, llm_result)
            reviewed_records.append(result)
            row_results.append(_build_row_result(label, row, result, gt_col))

        job["rows"] = row_results

        # Compute confusion metrics
        metrics = compute_confusion_metrics(row_results)
        job["metrics"] = metrics

        # Write output xlsx
        review_df = pd.DataFrame(reviewed_records)
        for col in REVIEW_COLUMNS:
            if col not in review_df.columns:
                review_df[col] = ""
        output_df = pd.concat(
            [df.reset_index(drop=True), review_df[REVIEW_COLUMNS].reset_index(drop=True)],
            axis=1,
        )
        export_reviewed(output_df, output_path)
        job["status"] = "done"

        # Auto-log to Google Sheets (best-effort)
        all_errors: list = []
        for r in row_results:
            all_errors.extend(r["error_types"])
        error_counts = dict(Counter(all_errors))

        statuses = [r["agent_status"] for r in row_results]
        approved_count = sum(1 for s in statuses if "approved" in s.lower())
        nr_count = sum(1 for s in statuses if s.lower() == "needs review")
        rejected_count = sum(1 for s in statuses if s.lower() == "rejected")
        model_used = next((r["llm_model"] for r in row_results if r.get("llm_model")), "V1 only")

        log_run(
            job_id=job_id,
            metrics=metrics,
            model=model_used,
            error_type_counts=error_counts,
            approved=approved_count,
            needs_review=nr_count,
            rejected=rejected_count,
        )

    except Exception as exc:
        import traceback
        job["status"] = "error"
        job["error"] = str(exc)
        job["error_detail"] = traceback.format_exc()

    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass


# ── API routes ─────────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    use_llm: str = Form("true"),
):
    suffix = Path(file.filename or "file.xlsx").suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Only .xlsx and .xls files are supported.")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File exceeds the 10 MB limit.")

    tmp_dir = tempfile.mkdtemp(prefix="aptitude_")
    input_path = os.path.join(tmp_dir, f"input{suffix}")
    output_path = os.path.join(tmp_dir, "output.xlsx")
    with open(input_path, "wb") as fh:
        fh.write(content)

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "pending",
        "total": 0,
        "done": 0,
        "current": "",
        "start_time": time.time(),
        "rows": [],
        "output_path": output_path,
        "error": None,
        "metrics": None,
        "has_ground_truth": False,
    }

    threading.Thread(
        target=_run_job,
        args=(job_id, input_path, output_path, use_llm.lower() in {"true", "1", "yes"}),
        daemon=True,
    ).start()

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    elapsed = time.time() - job["start_time"]
    remaining: Optional[float] = None
    if job["done"] > 0 and job["total"] > job["done"]:
        rate = job["done"] / elapsed
        remaining = (job["total"] - job["done"]) / rate if rate > 0 else None
    return {
        "status": job["status"],
        "total": job["total"],
        "done": job["done"],
        "current": job["current"],
        "elapsed": round(elapsed, 1),
        "remaining": round(remaining) if remaining else None,
        "error": job.get("error"),
    }


@app.get("/api/jobs/{job_id}/results")
async def get_job_results(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] not in {"done", "error"}:
        raise HTTPException(status_code=202, detail="Job still running")

    rows = job.get("rows", [])
    statuses = [r["agent_status"] for r in rows]
    approved = sum(1 for s in statuses if "approved" in s.lower())
    needs_review = sum(1 for s in statuses if s.lower() == "needs review")
    rejected = sum(1 for s in statuses if s.lower() == "rejected")

    all_errors: list = []
    for r in rows:
        all_errors.extend(r["error_types"])
    common = Counter(all_errors).most_common(6)

    return {
        "job_id": job_id,
        "status": job["status"],
        "error": job.get("error"),
        "total": len(rows),
        "approved": approved,
        "needs_review": needs_review,
        "rejected": rejected,
        "common_issues": [{"type": t, "count": c} for t, c in common],
        "rows": rows,
        "metrics": job.get("metrics"),
    }


@app.get("/api/jobs/{job_id}/download")
async def download_results(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Results not ready yet")
    path = job["output_path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Output file missing")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"review_{job_id}.xlsx",
    )


class FeedbackBody(BaseModel):
    sno: str
    verdict: str    # "approve" or "reject"
    remarks: str = ""


@app.post("/api/jobs/{job_id}/feedback")
async def submit_feedback(job_id: str, body: FeedbackBody):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job not finished yet")
    if body.verdict not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="verdict must be 'approve' or 'reject'")

    # Find the row and update in-memory
    row = next((r for r in job.get("rows", []) if r["sno"] == body.sno), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Question {body.sno} not found")

    row["user_verdict"] = body.verdict
    row["user_remarks"] = body.remarks

    # Log to Google Sheets (best-effort)
    log_feedback(
        job_id=job_id,
        sno=body.sno,
        agent_status=row.get("agent_status", ""),
        verdict=body.verdict,
        remarks=body.remarks,
        question=row.get("question", ""),
    )

    return {"ok": True}


@app.get("/api/template")
async def download_template():
    """Return a minimal blank template xlsx."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Questions"
    ws.append(REQUIRED_COLUMNS)
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return FileResponse(
        tmp.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="aptitude_template.xlsx",
    )


@app.get("/api/dashboard")
async def get_dashboard_data():
    """Return dashboard data: in-session jobs + historical runs from Google Sheets."""
    # Current session jobs (in-memory)
    session_runs = []
    for jid, job in jobs.items():
        if job["status"] != "done":
            continue
        rows = job.get("rows", [])
        statuses = [r["agent_status"] for r in rows]
        approved = sum(1 for s in statuses if "approved" in s.lower())
        needs_review = sum(1 for s in statuses if s.lower() == "needs review")
        rejected = sum(1 for s in statuses if s.lower() == "rejected")
        model = next((r.get("llm_model") for r in rows if r.get("llm_model")), "V1 only")
        session_runs.append({
            "job_id": jid,
            "timestamp": "",
            "model": model,
            "total": len(rows),
            "approved": approved,
            "needs_review": needs_review,
            "rejected": rejected,
            "metrics": job.get("metrics"),
            "source": "session",
        })

    # Historical runs from Google Sheets
    sheet_runs = []
    if sheets_configured():
        history = fetch_run_history()
        for rec in history:
            sheet_runs.append({
                "job_id": rec.get("Job ID", ""),
                "timestamp": rec.get("Timestamp", ""),
                "model": rec.get("Model", ""),
                "total": rec.get("Total", 0),
                "approved": rec.get("Approved", 0),
                "needs_review": rec.get("Needs Review", 0),
                "rejected": rec.get("Rejected", 0),
                "metrics": {
                    "has_ground_truth": rec.get("Has Ground Truth", "No") == "Yes",
                    "TP": rec.get("TP", 0),
                    "FP": rec.get("FP", 0),
                    "FN": rec.get("FN", 0),
                    "TN": rec.get("TN", 0),
                    "precision": rec.get("Precision", None),
                    "recall": rec.get("Recall", None),
                    "f1": rec.get("F1", None),
                    "accuracy": rec.get("Accuracy", None),
                },
                "source": "sheets",
            })

    all_runs = sheet_runs + session_runs

    # Aggregate error type counts across all runs (from Sheets + session)
    error_totals: Counter = Counter()
    if sheets_configured():
        for rec in fetch_error_history():
            etype = rec.get("Error Type", "")
            cnt = rec.get("Count", 0)
            if etype:
                try:
                    error_totals[etype] += int(cnt)
                except (TypeError, ValueError):
                    pass

    # Also include session-run error types from in-memory rows
    for jid, job in jobs.items():
        if job["status"] != "done":
            continue
        for r in job.get("rows", []):
            for et in r.get("error_types", []):
                error_totals[et] += 1

    top_errors = [{"type": t, "count": c} for t, c in error_totals.most_common(15)]

    return {
        "runs": all_runs,
        "sheets_configured": sheets_configured(),
        "top_errors": top_errors,
    }


# ── Serve frontend SPA ─────────────────────────────────────────────────────────
_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
