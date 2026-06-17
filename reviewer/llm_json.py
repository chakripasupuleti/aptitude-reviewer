import json
import re
from typing import Any, Dict, List, Optional


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from a model response."""
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    candidate = cleaned[start : end + 1]
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def extract_json_array(text: str) -> List[Any]:
    """Extract the first JSON array from a model response.

    Raises ValueError if no valid array is found.
    """
    if not text:
        raise ValueError("Empty response")

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, list):
            return obj
    except json.JSONDecodeError:
        pass

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON array found in response")

    candidate = cleaned[start : end + 1]
    try:
        obj = json.loads(candidate)
        if isinstance(obj, list):
            return obj
        raise ValueError("Parsed value is not a list")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON array: {exc}") from exc
