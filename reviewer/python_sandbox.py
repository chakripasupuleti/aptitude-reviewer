"""Safe Python arithmetic evaluator using a subprocess sandbox (Monty)."""
import re
import subprocess
import sys
from fractions import Fraction
from typing import Optional

_ARITHMETIC_ONLY = re.compile(r"^[\d\s+\-*/().,]+$")


def safe_eval_expr(expr: str, timeout: float = 2.0) -> Optional[Fraction]:
    """Evaluate an arithmetic expression in an isolated subprocess.

    Only accepts expressions containing digits and arithmetic operators.
    Returns None if the expression is unsafe, unparseable, or times out.
    """
    clean = (
        expr.strip()
        .replace(",", "")
        .replace("×", "*")   # ×
        .replace("÷", "/")   # ÷
        .replace("−", "-")   # −
    )
    if not clean or not _ARITHMETIC_ONLY.match(clean):
        return None

    code = (
        "from fractions import Fraction\n"
        f"val = eval({clean!r})\n"
        "r = Fraction(val).limit_denominator(100000)\n"
        "print(r.numerator, r.denominator)"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        parts = proc.stdout.strip().split()
        if len(parts) != 2:
            return None
        return Fraction(int(parts[0]), int(parts[1]))
    except Exception:
        return None
