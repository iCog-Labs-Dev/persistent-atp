"""Child process exercising the installed SDK plugin against a mock endpoint."""
import json
import os
import sys

sys.path.insert(0, os.environ["OMEGA_TEST_ROOT"] + "/src")
import config
import plugin
import providers

config.init_config([])
plugin.loadPythonPlugin("persistent_atp_providers", os.environ["OMEGA_PROVIDER_PLUGIN_DIR"])
for name in ("OpenAI", "DeepSeek", "OpenRouter", "Custom"):
    assert "PersistentATP-" + name in providers._llmProviderRegistry

try:
    providers.llmProviderStart("PersistentATP-OpenAI")
    raise AssertionError("missing OpenAI key was accepted")
except RuntimeError as exc:
    assert "OPENAI_API_KEY" in str(exc)

tool = providers.LLMTool().with_name("proof-status").with_description("read proof") \
    .with_parameters([providers.LLMToolParameter().with_name("proof"),
                      providers.LLMToolParameter().with_name("workspace")])
request = providers.llmRequest(
    providers.llmRequestMessage("system", "Read committed proof status"),
    [providers.llmRequestMessage("user", "Check p1")], 128, "low", [tool])
providers.llmProviderStart("PersistentATP-Custom")
first = providers.llmProviderChat(request)
assert not first.is_error() and first.calls[0].name == "proof-status", first
followup = providers.llmRequest(
    providers.llmRequestMessage("system", "Read committed proof status"),
    [providers.llmRequestMessage("user", "Check p1"),
     providers.llmToolCallMessage("assistant", first.calls, "[TOOL CALL]"),
     providers.llmToolCallResponseMessage("tool", first.calls[0].id,
         json.dumps({"event_hash": "sha256:committed"}))], 128, "low", [tool])
second = providers.llmProviderChat(followup)
assert not second.is_error() and second.calls[0].name == "proof-status", second
malformed = providers.llmProviderChat(request)
assert malformed.is_error() and malformed.error == "PROVIDER_PROTOCOL_ERROR", malformed

config.init_config(["GATEWAY_URL=" + os.environ["OMEGA_TEST_GATEWAY"]])
providers.llmProviderStart("PersistentATP-DeepSeek")
deepseek = providers.llmProviderChat(request)
assert not deepseek.is_error() and deepseek.calls[0].name == "proof-status", deepseek
providers.llmProviderStart("PersistentATP-OpenAI")
openai = providers.llmProviderChat(request)
assert not openai.is_error() and openai.calls[0].name == "proof-status", openai
providers.llmProviderStart("PersistentATP-OpenRouter")
openrouter = providers.llmProviderChat(request)
assert not openrouter.is_error() and openrouter.calls[0].name == "proof-status", openrouter
print("SDK_PROVIDER_RESULT=" + json.dumps({"registered": 4,
    "custom_roundtrips": 2, "rejected_malformed": True,
    "gateway_profiles": ["deepseek", "openai", "openrouter"]}), flush=True)
