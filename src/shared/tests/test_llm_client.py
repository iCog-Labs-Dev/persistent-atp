"""Structured-output recovery for the shared Gemini client."""

import json
from unittest.mock import patch

from shared.llm_client import GeminiLLMClient


def test_gemini_retries_truncated_json_with_compact_instruction():
    client = GeminiLLMClient(api_key="test", json_attempts=2)
    replies = iter(['{"moves": [', '{"moves": [], "reasoning": "concise"}'])
    prompts = []

    def generate(system_prompt, _user_prompt):
        prompts.append(system_prompt)
        return next(replies)

    client.generate = generate
    result = client.generate_json("worker instructions", "packet")

    assert result == {"moves": [], "reasoning": "concise"}
    assert len(prompts) == 2
    assert "invalid or truncated JSON" in prompts[1]


def test_gemini_sends_explicit_output_token_budget():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "{}"}]},
                }],
            }).encode()

    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    client = GeminiLLMClient(api_key="test", max_output_tokens=4096)
    with patch("shared.llm_client.urllib.request.urlopen", urlopen):
        assert client.generate("system", "user") == "{}"

    payload = json.loads(requests[0][0].data)
    assert payload["generationConfig"]["maxOutputTokens"] == 4096
