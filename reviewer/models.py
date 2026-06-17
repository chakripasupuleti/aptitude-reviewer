from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewIssue:
    severity: str
    error_type: str
    field: str
    evidence: str
    reason: str
    suggested_fix: str = ""
    confidence: str = "High"

    def to_remark(self) -> str:
        evidence_part = f' Evidence: "{self.evidence}".' if self.evidence else ""
        fix_part = f" Suggested fix: {self.suggested_fix}." if self.suggested_fix else ""
        return f"[{self.severity} | {self.error_type}] {self.reason}{evidence_part}{fix_part}"

    def dedupe_key(self) -> tuple:
        return (
            self.severity,
            self.error_type,
            self.field,
            self.evidence.strip().lower(),
            self.reason.strip().lower(),
        )


def make_issue(
    severity: str,
    error_type: str,
    field: str,
    evidence: str,
    reason: str,
    suggested_fix: str = "",
    confidence: str = "High",
) -> ReviewIssue:
    return ReviewIssue(
        severity=severity,
        error_type=error_type,
        field=field,
        evidence=str(evidence or ""),
        reason=reason,
        suggested_fix=suggested_fix,
        confidence=confidence,
    )
