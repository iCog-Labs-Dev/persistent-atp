"""Small live DeepSeek provider probe; no proof state is changed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def main() -> int:
    omega = Path(os.environ.get("OMEGA_PINNED_ROOT", str(PROJECT_ROOT / "tmp" / "repos" / "Omega")))
    if not (omega / "src" / "providers.py").is_file():
        raise SystemExit("OMEGA_PINNED_ROOT must name the pinned Omega checkout")
    sys.path.insert(0, str(omega / "src"))
    import plugin
    import providers

    plugin.loadPythonPlugin("deepseek_provider", str(PACKAGE_ROOT / "omega_plugin"))
    providers.llmProviderStart("DeepSeek")
    tool = providers.LLMTool().with_name("proof-status").with_description(
        "Read committed proof status").with_parameters([
            providers.LLMToolParameter().with_name("proof"),
            providers.LLMToolParameter().with_name("workspace")])
    request = providers.llmRequest(
        providers.llmRequestMessage("system", "Use the proof-status tool for the requested proof."),
        [providers.llmRequestMessage("user", "Check proof demo in workspace reference")],
        128, "low", [tool])
    response = providers.llmProviderChat(request)
    print(json.dumps({"mode": "live-deepseek", "error": response.error,
        "calls": [{"name": call.name, "arguments": call.arguments, "error": call.error}
                  for call in response.calls]}, sort_keys=True))
    return int(response.is_error())


if __name__ == "__main__":
    raise SystemExit(main())
