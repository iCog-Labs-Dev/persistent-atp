"""Small live Omega provider probe; no proof state is changed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import providers as compatible_providers


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe an Omega tool-call provider")
    parser.add_argument("--provider", choices=(*compatible_providers.PROFILES, "deepseek-legacy"),
                        default="deepseek")
    parser.add_argument("--proof", default="demo")
    parser.add_argument("--workspace", default="reference")
    args = parser.parse_args(argv)
    omega = Path(os.environ.get("OMEGA_PINNED_ROOT", str(PROJECT_ROOT / "tmp" / "repos" / "Omega")))
    if not (omega / "src" / "providers.py").is_file():
        raise SystemExit("OMEGA_PINNED_ROOT must name the pinned Omega checkout")
    sys.path.insert(0, str(omega / "src"))
    import plugin
    import providers

    if args.provider == "deepseek-legacy":
        plugin.loadPythonPlugin("deepseek_provider", str(PACKAGE_ROOT / "omega_plugin"))
        provider_name = "DeepSeek"
    else:
        compatible_providers.loadOmegaPlugin()
        provider_name = compatible_providers.PROFILES[args.provider].name
    providers.llmProviderStart(provider_name)
    tool = providers.LLMTool().with_name("proof-status").with_description(
        "Read committed proof status").with_parameters([
            providers.LLMToolParameter().with_name("proof"),
            providers.LLMToolParameter().with_name("workspace")])
    request = providers.llmRequest(
        providers.llmRequestMessage("system", "Use the proof-status tool for the requested proof."),
        [providers.llmRequestMessage("user", f"Check proof {args.proof} in workspace {args.workspace}")],
        128, "low", [tool])
    response = providers.llmProviderChat(request)
    print(json.dumps({"mode": "live-" + args.provider, "error": response.error,
        "calls": [{"name": call.name, "arguments": call.arguments, "error": call.error}
                  for call in response.calls]}, sort_keys=True))
    return int(response.is_error())


if __name__ == "__main__":
    raise SystemExit(main())
