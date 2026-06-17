REQUIRED_COLUMNS = [
    "S. No",
    "Question",
    "Option A",
    "Option B",
    "Option C",
    "Option D",
    "Explanation",
    "Key",
]

OPTION_COLUMNS = ["Option A", "Option B", "Option C", "Option D"]

VALID_KEYS = ["Option A", "Option B", "Option C", "Option D"]

REVIEW_COLUMNS = [
    "Agent Status",
    "Error Count",
    "Error Types",
    "Agent Remarks",
    "Suggested Corrections",
    "Confidence",
    "Reviewed Key",
    "LLM Status",
    "LLM Error",
    "LLM Model",
]

SEVERITY_ORDER = {
    "Critical": 0,
    "Major": 1,
    "Minor": 2,
    "Suggestion": 3,
}

STATUS_APPROVED = "Approved"
STATUS_APPROVED_MINOR = "Approved with Minor Fixes"
STATUS_NEEDS_REVIEW = "Needs Review"
STATUS_REJECTED = "Rejected"
