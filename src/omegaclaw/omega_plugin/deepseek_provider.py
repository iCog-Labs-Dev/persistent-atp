"""DeepSeek Chat Completions provider for the pinned Omega tool-call API.

Omega does not retain ``reasoning_content`` in its message history, so this
provider explicitly disables thinking. It makes no proof-status decisions.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from config import config_get_by_key
import providers


DEFAULT_URL = "https://api.deepseek.com/chat/completions"
SUPPORTED_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
MAX_RESPONSE_BYTES = 1024 * 1024


class DeepSeekProvider(providers.LLMProvider):
    def __init__(self):
        self.api_key = None
        self.model = None
        self.url = None
        self.gateway = False

    def start(self):
        model = config_get_by_key("model", os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
        if model not in SUPPORTED_MODELS:
            raise RuntimeError("DEEPSEEK_MODEL is not a supported non-thinking tool-call model")
        gateway = config_get_by_key("GATEWAY_URL")
        if gateway:
            if os.environ.get("DEEPSEEK_API_URL"):
                raise RuntimeError("DEEPSEEK_API_URL cannot override the configured Omega gateway")
            parsed = urllib.parse.urlparse(gateway)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname \
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}:
                raise RuntimeError("GATEWAY_URL is invalid for the DeepSeek provider")
            self.api_key, self.model = None, model
            self.url = gateway.rstrip("/") + "/deepseek/chat/completions"
            self.gateway = True
        else:
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not key or "\n" in key or "\r" in key:
                raise RuntimeError("DEEPSEEK_API_KEY is missing or invalid")
            url = os.environ.get("DEEPSEEK_API_URL", DEFAULT_URL)
            if url.rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
                url = DEFAULT_URL
            parsed = urllib.parse.urlparse(url)
            if url != DEFAULT_URL:
                if not (os.environ.get("DEEPSEEK_TEST_MODE") == "1"
                        and parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "localhost"}
                        and parsed.path == "/chat/completions"
                        and not parsed.username and not parsed.password):
                    raise RuntimeError("DEEPSEEK_API_URL override is allowed only for a loopback test endpoint")
            self.api_key, self.model, self.url = key, model, url
            self.gateway = False

    def stop(self):
        self.api_key = None
        self.url = None
        self.gateway = False

    @staticmethod
    def _messages(messages):
        converted = []
        for message in messages:
            if isinstance(message, providers.LLMToolCallResponseMessage):
                if message.role != "tool" or not message.callid or not isinstance(message.content, str):
                    raise ValueError("invalid tool response message")
                converted.append({"role": "tool", "tool_call_id": message.callid,
                                  "content": message.content})
            elif isinstance(message, providers.LLMToolCallMessage):
                if message.role != "assistant" or not message.calls:
                    raise ValueError("invalid assistant tool-call message")
                converted.append({"role": "assistant", "content": message.content,
                    "tool_calls": [{"id": call.id, "type": "function",
                                    "function": {"name": call.name,
                                                 "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                                   for call in message.calls]})
            elif message.role in {"system", "user", "assistant"} and isinstance(message.content, str):
                converted.append({"role": message.role, "content": message.content})
            else:
                raise ValueError("invalid Omega request message")
        return converted

    @staticmethod
    def _tools(tools):
        result = []
        for tool in tools:
            names = [parameter.name for parameter in tool.parameters]
            if not tool.name or len(set(names)) != len(names) or not all(names):
                raise ValueError("invalid Omega tool definition")
            result.append({"type": "function", "function": {
                "name": tool.name, "description": tool.description or "",
                "parameters": {"type": "object",
                    "properties": {name: {"type": "string"} for name in names},
                    "required": names, "additionalProperties": False}}})
        if not result:
            raise ValueError("DeepSeek Omega provider requires at least one tool")
        return result

    def chat(self, request):
        if not self.url or (not self.gateway and not self.api_key):
            return providers.LLMResponse().with_error("DEEPSEEK_NOT_STARTED")
        try:
            if isinstance(request.max_tokens, bool) or not isinstance(request.max_tokens, int) \
                    or not 1 <= request.max_tokens <= 8192:
                raise ValueError("max_tokens must be an integer in [1, 8192]")
            body = {"model": self.model, "messages": self._messages(request.messages),
                    "max_tokens": request.max_tokens, "thinking": {"type": "disabled"},
                    "tools": self._tools(request.tools), "tool_choice": "required"}
            wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if not self.gateway:
                headers["Authorization"] = "Bearer " + self.api_key
            outgoing = urllib.request.Request(self.url, data=wire, method="POST", headers=headers)
            with urllib.request.urlopen(outgoing, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("DeepSeek response exceeds size limit")
            payload = json.loads(raw)
            choice = payload["choices"][0]
            if choice["finish_reason"] != "tool_calls":
                raise ValueError("DeepSeek did not finish with tool calls")
            message = choice["message"]
            if message["role"] != "assistant":
                raise ValueError("DeepSeek returned an unexpected message role")
            if message.get("reasoning_content"):
                raise ValueError("DeepSeek returned reasoning content in disabled mode")
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls:
                raise ValueError("DeepSeek returned no tool calls")
            result = providers.LLMResponse()
            known = {tool.name: {p.name for p in tool.parameters} for tool in request.tools}
            seen_ids = set()
            for item in calls:
                if item["type"] != "function" or not isinstance(item["id"], str) or not item["id"]:
                    raise ValueError("invalid DeepSeek tool-call identity")
                if item["id"] in seen_ids:
                    raise ValueError("duplicate DeepSeek tool-call identity")
                seen_ids.add(item["id"])
                fn = item["function"]
                name = fn["name"]
                args = json.loads(fn["arguments"])
                if name not in known or not isinstance(args, dict) or set(args) != known[name] \
                        or not all(isinstance(v, str) for v in args.values()):
                    raise ValueError("invalid DeepSeek tool-call arguments")
                result.add_tool_call(providers.LLMToolCall().with_id(item["id"])
                    .with_name(name).with_arguments(args))
            return result
        except urllib.error.HTTPError as exc:
            return providers.LLMResponse().with_error(f"DEEPSEEK_HTTP_{exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError):
            return providers.LLMResponse().with_error("DEEPSEEK_UNAVAILABLE")
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError, json.JSONDecodeError):
            return providers.LLMResponse().with_error("DEEPSEEK_PROTOCOL_ERROR")


def loadOmegaPlugin():
    providers.registerLLMProvider("DeepSeek", DeepSeekProvider())
