"""OpenAI SDK backed, tool-only providers for Omega's LLM provider registry.

The installer copies this file into a derived Omega plugin directory as
``persistent_atp_providers.py``.  That name is deliberate: Omega already has a
core module named ``providers`` and its registry must never be shadowed.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.parse import urlparse


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    key_env: str
    model_env: str
    base_url: str | None
    default_model: str | None = None
    gateway_prefix: str | None = None


PROFILES = {
    "openai": ProviderProfile("PersistentATP-OpenAI", "OPENAI_API_KEY",
        "OMEGACLAW_OPENAI_MODEL", "https://api.openai.com/v1", gateway_prefix="openai"),
    "deepseek": ProviderProfile("PersistentATP-DeepSeek", "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL", "https://api.deepseek.com", "deepseek-flash", "deepseek"),
    "openrouter": ProviderProfile("PersistentATP-OpenRouter", "OPENROUTER_API_KEY",
        "OMEGACLAW_OPENROUTER_MODEL", "https://openrouter.ai/api/v1",
        gateway_prefix="openrouter"),
    "custom": ProviderProfile("PersistentATP-Custom", "OMEGACLAW_CUSTOM_API_KEY",
        "OMEGACLAW_CUSTOM_MODEL", None),
}


def _valid_base_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == "https" or
            (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})) \
        and bool(parsed.hostname) and not parsed.username and not parsed.password \
        and not parsed.query and not parsed.fragment and port != 0


class OpenAICompatibleProvider:
    """Translate Omega tool turns to bounded Chat Completions requests."""

    def __init__(self, profile: ProviderProfile):
        self.profile = profile
        self.client = None
        self.model = None

    def start(self):
        self.stop()
        # Import lazily: the legacy DeepSeek and PeTTa process path works without
        # the optional SDK. Selecting this provider must fail explicitly if absent.
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenAI SDK is missing in Omega's Python; install openai>=2,<3 "
                               "for that interpreter") from exc
        profile = self.profile
        from config import config_get_by_key
        model = os.environ.get(profile.model_env) or config_get_by_key("model") or profile.default_model
        if not isinstance(model, str) or not model.strip() or any(c in model for c in "\r\n"):
            raise RuntimeError(f"{profile.model_env} must specify a model")
        gateway = config_get_by_key("GATEWAY_URL")
        if gateway:
            if not profile.gateway_prefix or not _valid_base_url(gateway):
                raise RuntimeError("GATEWAY_URL is invalid for this provider")
            base_url = gateway.rstrip("/") + "/" + profile.gateway_prefix
            key = "proxy"
        else:
            key = os.environ.get(profile.key_env, "")
            if not key or any(c in key for c in "\r\n"):
                raise RuntimeError(f"{profile.key_env} is missing or invalid")
            base_url = (os.environ.get("OMEGACLAW_CUSTOM_BASE_URL") if profile.base_url is None
                        else profile.base_url)
            if not base_url or not _valid_base_url(base_url):
                raise RuntimeError("provider base URL is missing or invalid")
        self.client = OpenAI(api_key=key, base_url=base_url,
                             max_retries=0, timeout=30.0)
        self.model = model

    def stop(self):
        if self.client is not None:
            self.client.close()
        self.client = None
        self.model = None

    @staticmethod
    def _messages(messages, omega):
        result = []
        for message in messages:
            if isinstance(message, omega.LLMToolCallResponseMessage):
                if message.role != "tool" or not message.callid or not isinstance(message.content, str):
                    raise ValueError("invalid tool result")
                result.append({"role": "tool", "tool_call_id": message.callid,
                               "content": message.content})
            elif isinstance(message, omega.LLMToolCallMessage):
                if message.role != "assistant" or not message.calls:
                    raise ValueError("invalid assistant tool calls")
                calls = []
                for call in message.calls:
                    if not call.id or not call.name or not isinstance(call.arguments, dict):
                        raise ValueError("invalid prior tool call")
                    calls.append({"id": call.id, "type": "function", "function": {
                        "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}})
                result.append({"role": "assistant", "content": message.content,
                               "tool_calls": calls})
            elif message.role in {"system", "user", "assistant"} and isinstance(message.content, str):
                result.append({"role": message.role, "content": message.content})
            else:
                raise ValueError("invalid Omega message")
        return result

    @staticmethod
    def _tools(tools):
        if not tools:
            raise ValueError("Omega tool request must expose at least one tool")
        result = []
        tool_names = set()
        for tool in tools:
            names = [parameter.name for parameter in tool.parameters]
            if not tool.name or tool.name in tool_names or len(set(names)) != len(names) \
                    or not all(names):
                raise ValueError("invalid Omega tool")
            tool_names.add(tool.name)
            result.append({"type": "function", "function": {
                "name": tool.name, "description": tool.description or "",
                "parameters": {"type": "object",
                    "properties": {name: {"type": "string"} for name in names},
                    "required": names, "additionalProperties": False}}})
        return result

    def chat(self, request):
        import providers as omega
        if self.client is None:
            return omega.LLMResponse().with_error("PROVIDER_NOT_STARTED")
        try:
            if isinstance(request.max_tokens, bool) or not isinstance(request.max_tokens, int) \
                    or not 1 <= request.max_tokens <= 8192:
                raise ValueError("invalid output token bound")
            kwargs = {"model": self.model,
                      "messages": self._messages(request.messages, omega),
                      "tools": self._tools(request.tools), "tool_choice": "required"}
            if self.profile.name == "PersistentATP-OpenAI":
                kwargs["max_completion_tokens"] = request.max_tokens
            else:
                kwargs["max_tokens"] = request.max_tokens
            if self.profile.name == "PersistentATP-DeepSeek":
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            raw = self.client.chat.completions.create(**kwargs)
            if len(raw.choices) != 1 or raw.choices[0].finish_reason != "tool_calls":
                raise ValueError("model did not finish with tool calls")
            message = raw.choices[0].message
            if message.role != "assistant" or getattr(message, "reasoning_content", None):
                raise ValueError("unexpected assistant response")
            if not message.tool_calls:
                raise ValueError("model returned no tool calls")
            known = {tool.name: {parameter.name for parameter in tool.parameters}
                     for tool in request.tools}
            seen = set()
            result = omega.LLMResponse()
            for item in message.tool_calls:
                if item.type != "function" or not item.id or item.id in seen:
                    raise ValueError("invalid tool call identity")
                seen.add(item.id)
                name = item.function.name
                args = json.loads(item.function.arguments)
                if name not in known or not isinstance(args, dict) or set(args) != known[name] \
                        or not all(isinstance(value, str) for value in args.values()):
                    raise ValueError("invalid tool call arguments")
                result.add_tool_call(omega.LLMToolCall().with_id(item.id)
                    .with_name(name).with_arguments(args))
            return result
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, json.JSONDecodeError):
            return omega.LLMResponse().with_error("PROVIDER_PROTOCOL_ERROR")
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                return omega.LLMResponse().with_error(f"PROVIDER_HTTP_{status}")
            return omega.LLMResponse().with_error("PROVIDER_UNAVAILABLE")


def loadOmegaPlugin():
    import providers as omega
    for profile in PROFILES.values():
        omega.registerLLMProvider(profile.name, OpenAICompatibleProvider(profile))
