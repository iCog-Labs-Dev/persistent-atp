"""Compatibility imports for existing alignment integrations."""

from shared.llm_client import (
    CallableLLMClient,
    GeminiLLMClient,
    HTTPLLMClient,
    LLMClientProtocol,
    MockLLMClient,
    extract_json_from_text,
)

__all__ = [
    "CallableLLMClient",
    "GeminiLLMClient",
    "HTTPLLMClient",
    "LLMClientProtocol",
    "MockLLMClient",
    "extract_json_from_text",
]
