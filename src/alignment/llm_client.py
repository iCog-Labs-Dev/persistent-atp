"""Production LLM client abstractions for alignment and formalization services."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

from dotenv import load_dotenv

load_dotenv()

__all__ = [
    "CallableLLMClient",
    "GeminiLLMClient",
    "HTTPLLMClient",
    "LLMClientProtocol",
    "MockLLMClient",
    "extract_json_from_text",
]


def extract_json_from_text(text: str) -> dict[str, Any]:
    """Robustly extract and parse a JSON object from raw LLM output."""
    cleaned = text.strip()

    # Try direct parse first
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences: ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # Try finding outermost braces { ... }
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        try:
            data = json.loads(cleaned[brace_start : brace_end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse valid JSON from LLM response:\n{text}")


class LLMClientProtocol(Protocol):
    """Protocol for LLM clients used by formalization and alignment workers."""

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Generate raw text completion."""
        ...

    def generate_json(
        self, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        """Generate and parse structured JSON response."""
        ...


class HTTPLLMClient:
    """Production HTTP client compatible with OpenAI, DeepSeek, vLLM, and Ollama APIs."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        api_key: str | None = None,
        model: str = "deepseek-chat",
        timeout_seconds: float = 60.0,
        temperature: float = 0.0,
    ) -> None:
        self.endpoint_url = (
            endpoint_url
            or os.getenv("LLM_ENDPOINT_URL")
            or "https://api.openai.com/v1/chat/completions"
        )
        self.api_key = (
            api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        )
        self.model = model or os.getenv("LLM_MODEL", "deepseek-chat")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Call chat completions endpoint."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
        }

        req = urllib.request.Request(
            self.endpoint_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                res_body = response.read().decode("utf-8")
                res_json = json.loads(res_body)
                content = res_json["choices"][0]["message"]["content"]
                return str(content)
        except urllib.error.HTTPError as exc:
            err_msg = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(
                f"LLM API request failed with HTTP {exc.code}: {err_msg}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"LLM API request failed: {exc}") from exc

    def generate_json(
        self, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        """Generate response and parse JSON."""
        raw_text = self.generate(system_prompt, user_prompt)
        return extract_json_from_text(raw_text)


class CallableLLMClient:
    """Adapter wrapping any custom Python function or SDK callable."""

    def __init__(
        self,
        call_fn: Callable[[str, str], str | dict[str, Any]],
    ) -> None:
        self.call_fn = call_fn

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        res = self.call_fn(system_prompt, user_prompt)
        if isinstance(res, dict):
            return json.dumps(res)
        return str(res)

    def generate_json(
        self, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        res = self.call_fn(system_prompt, user_prompt)
        if isinstance(res, dict):
            return res
        return extract_json_from_text(str(res))


class MockLLMClient:
    """Configurable mock LLM client for deterministic testing."""

    def __init__(
        self,
        default_json: dict[str, Any] | None = None,
        responses: list[str | dict[str, Any]] | None = None,
    ) -> None:
        self.default_json = default_json or {}
        self.responses = list(responses or [])
        self.history: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.history.append((system_prompt, user_prompt))
        if self.responses:
            val = self.responses.pop(0)
            if isinstance(val, dict):
                return json.dumps(val)
            return str(val)
        return json.dumps(self.default_json)

    def generate_json(
        self, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        self.history.append((system_prompt, user_prompt))
        if self.responses:
            val = self.responses.pop(0)
            if isinstance(val, dict):
                return val
            return extract_json_from_text(str(val))
        return dict(self.default_json)


class GeminiLLMClient:
    """Production client calling Google Gemini models directly via Google Generative Language API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-flash-lite-latest",
        timeout_seconds: float = 45.0,
        temperature: float = 0.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or ""
        )
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY not found in environment or constructor."
            )
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_retries = max_retries

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Call Gemini generateContent endpoint."""
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        }
        if system_prompt.strip():
            payload["system_instruction"] = {
                "parts": [{"text": system_prompt}]
            }

        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(
                url, data=data_bytes, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    res_body = resp.read().decode("utf-8")
                    res_json = json.loads(res_body)
                    candidates = res_json.get("candidates", [])
                    if not candidates:
                        raise RuntimeError(f"Gemini returned no candidates: {res_json}")
                    text = candidates[0]["content"]["parts"][0]["text"]
                    return str(text)
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8") if exc.fp else str(exc)
                last_error = RuntimeError(f"Gemini API error (HTTP {exc.code}): {err_body}")
                if exc.code in (429, 500, 503):
                    time.sleep(1.5 * attempt)
                    continue
                raise last_error from exc
            except Exception as exc:
                last_error = exc
                time.sleep(1.0 * attempt)

        raise RuntimeError(
            f"Gemini API failed after {self.max_retries} attempts: {last_error}"
        ) from last_error

    def generate_json(
        self, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        """Generate response and parse into structured JSON."""
        raw_text = self.generate(system_prompt, user_prompt)
        return extract_json_from_text(raw_text)

