"""Google Sheets logger for review run history.

Requires environment variables:
  GOOGLE_SHEET_ID          — the ID from the Sheet URL
  GOOGLE_CREDENTIALS_PATH  — path to the service-account credentials JSON (default: credentials.json)

If either var is missing the module is a no-op and logs a warning instead of crashing.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_RUNS_HEADERS = [
    "Timestamp", "Job ID", "Model", "Total",
    "TP", "FP", "FN", "TN",
    "Precision", "Recall", "F1", "Accuracy",
    "Has Ground Truth",
    "Approved", "Needs Review", "Rejected",
]

_ERRORS_HEADERS = ["Timestamp", "Job ID", "Error Type", "Count"]

_FEEDBACK_HEADERS = [
    "Timestamp", "Job ID", "Question No", "Question",
    "Agent Status", "User Verdict", "User Remarks",
]


def _get_client():
    """Return an authenticated gspread client, or None if not configured."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json").strip()

    if not sheet_id:
        return None, None

    try:
        import json
        import gspread
        from google.oauth2.service_account import Credentials

        # Detect wrong credential type early with a helpful message
        try:
            with open(creds_path) as f:
                raw = json.load(f)
            if "web" in raw or "installed" in raw:
                logger.warning(
                    "credentials.json is an OAuth web/installed-app credentials file, "
                    "not a service account key. Go to Google Cloud Console → "
                    "IAM & Admin → Service Accounts → Keys → Add Key → JSON, "
                    "then replace credentials.json with that file."
                )
                return None, None
        except (OSError, json.JSONDecodeError):
            pass  # will be caught below

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)
        return client, spreadsheet
    except FileNotFoundError:
        logger.warning("Google credentials file not found at %s — Sheets logging disabled.", creds_path)
        return None, None
    except Exception as exc:
        logger.warning("Sheets login failed: %s — logging disabled.", exc)
        return None, None


def _ensure_sheet(spreadsheet, title: str, headers: List[str]):
    """Get or create a worksheet with the given headers in row 1."""
    try:
        ws = spreadsheet.worksheet(title)
    except Exception:
        try:
            ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.append_row(headers)
            return ws
        except Exception:
            # Sheet may have been created between the two calls — fetch it
            ws = spreadsheet.worksheet(title)

    # Ensure headers exist
    existing = ws.row_values(1) if ws.row_count > 0 else []
    if not existing:
        ws.append_row(headers)
    return ws


def log_run(
    job_id: str,
    metrics: Dict[str, Any],
    model: str,
    error_type_counts: Dict[str, int],
    approved: int,
    needs_review: int,
    rejected: int,
) -> bool:
    """Append one row to the Runs sheet and error-type rows to the ErrorTypes sheet.

    Returns True if logging succeeded, False otherwise.
    """
    _, spreadsheet = _get_client()
    if spreadsheet is None:
        return False

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        runs_ws = _ensure_sheet(spreadsheet, "Runs", _RUNS_HEADERS)
        runs_ws.append_row([
            ts,
            job_id,
            model or "V1 only",
            metrics.get("total", 0),
            metrics.get("TP", 0),
            metrics.get("FP", 0),
            metrics.get("FN", 0),
            metrics.get("TN", 0),
            metrics.get("precision", ""),
            metrics.get("recall", ""),
            metrics.get("f1", ""),
            metrics.get("accuracy", ""),
            "Yes" if metrics.get("has_ground_truth") else "No",
            approved,
            needs_review,
            rejected,
        ])

        if error_type_counts:
            errors_ws = _ensure_sheet(spreadsheet, "Errors", _ERRORS_HEADERS)
            errors_ws.append_rows([
                [ts, job_id, etype, count]
                for etype, count in error_type_counts.items()
            ])

        logger.info("Logged job %s to Google Sheets.", job_id)
        return True

    except Exception as exc:
        logger.warning("Failed to log to Sheets: %s", exc)
        return False


def fetch_run_history() -> List[Dict]:
    """Return all rows from the Runs sheet as a list of dicts.

    Returns [] if Sheets is not configured or fetch fails.
    """
    _, spreadsheet = _get_client()
    if spreadsheet is None:
        return []

    try:
        ws = spreadsheet.worksheet("Runs")
        records = ws.get_all_records()
        return records
    except Exception as exc:
        logger.warning("Failed to fetch run history from Sheets: %s", exc)
        return []


def fetch_error_history() -> List[Dict]:
    """Return all rows from the Errors sheet as a list of dicts.

    Returns [] if Sheets is not configured or fetch fails.
    """
    _, spreadsheet = _get_client()
    if spreadsheet is None:
        return []

    try:
        ws = spreadsheet.worksheet("Errors")
        records = ws.get_all_records()
        return records
    except Exception as exc:
        logger.warning("Failed to fetch error history from Sheets: %s", exc)
        return []


def log_feedback(
    job_id: str,
    sno: str,
    agent_status: str,
    verdict: str,
    remarks: str,
    question: str = "",
) -> bool:
    """Append one feedback row to the Feedback sheet.

    Returns True if logging succeeded, False otherwise.
    """
    _, spreadsheet = _get_client()
    if spreadsheet is None:
        return False

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        ws = _ensure_sheet(spreadsheet, "Feedback", _FEEDBACK_HEADERS)
        ws.append_row([ts, job_id, sno, question, agent_status, verdict, remarks or ""])
        logger.info("Logged feedback for job %s question %s to Google Sheets.", job_id, sno)
        return True
    except Exception as exc:
        logger.warning("Failed to log feedback to Sheets: %s", exc)
        return False


def sheets_configured() -> bool:
    """Return True if Google Sheets environment vars are set."""
    return bool(os.getenv("GOOGLE_SHEET_ID", "").strip())
