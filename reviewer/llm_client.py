import os
import time
from typing import List, Dict, Any, Optional

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"

# Backward-compatible aliases
DEFAULT_MISTRAL_MODEL = DEFAULT_MODEL

RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMClientError(RuntimeError):
    pass


# Backward-compatible alias
MistralClientError = LLMClientError


def load_environment() -> None:
    if load_dotenv is not None:
        load_dotenv(override=False)


def get_requested_model_name(model_override: Optional[str] = None) -> str:
    """Return the model ID that will be used."""
    load_environment()
    raw = (model_override or os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()
    return raw


def normalize_model_name(model_name: Optional[str]) -> str:
    return get_requested_model_name(model_name)


def _get_config(model_override: Optional[str] = None) -> tuple[str, str]:
    load_environment()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = get_requested_model_name(model_override)
    if not api_key:
        raise LLMClientError(
            "OPENROUTER_API_KEY is not set. Add your OpenRouter API key to .env as OPENROUTER_API_KEY."
        )
    return api_key, model


def chat_completion(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1600,
    timeout: int = 60,
    retries: int = 4,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Send a chat completion request to OpenRouter.

    When `tools` is provided the model may respond with tool_calls.
    In that case the raw choice dict is returned instead of a plain string
    so the caller can execute the tools and continue the conversation.
    """
    api_key, resolved_model = _get_config(model)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://aptitude-reviewer",
        "X-Title": "Aptitude Content Reviewer",
    }
    payload: Dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools

    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=timeout
            )

            if response.status_code in RETRIABLE_STATUS_CODES and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else min(30, 2 ** attempt)
                except ValueError:
                    wait = min(30, 2 ** attempt)
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                raise LLMClientError(
                    f"OpenRouter API error {response.status_code}: {response.text[:500]}"
                )

            data = response.json()
            choice = data["choices"][0]
            # If the model called a tool, return the raw choice for the caller to handle.
            if choice.get("finish_reason") == "tool_calls":
                return choice
            return choice["message"]["content"]

        except LLMClientError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            break

    raise LLMClientError(f"OpenRouter API call failed: {last_error}")
