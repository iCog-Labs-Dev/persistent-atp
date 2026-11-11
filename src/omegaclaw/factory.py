"""Trusted startup composition. Request arguments cannot select dependencies."""
from __future__ import annotations

import os
from pathlib import Path

from .service import OperationService


def build_service(config: dict, root: Path):
    mode = config.get("mode", "reference")
    if mode == "reference":
        if set(config) - {"mode", "root", "workspace", "host", "port", "token_env"}:
            raise ValueError("production dependencies require explicit mode=production")
        return OperationService(root, workspace=config["workspace"])
    if mode != "production":
        raise ValueError("mode must be reference or production")
    allowed = {"root", "workspace", "host", "port", "token_env", "mode", "graph_backend",
               "artifact_db_env", "formal_provider", "snapshot"}
    if set(config) - allowed:
        raise ValueError("unknown production configuration fields")
    if config.get("graph_backend") != "mork":
        raise ValueError("production graph_backend must be mork")
    name = config.get("artifact_db_env")
    db_url = os.environ.get(name, "") if isinstance(name, str) else ""
    if not db_url:
        raise ValueError("configured artifact database environment variable is unset")
    provider = config.get("formal_provider")
    if not isinstance(provider, dict) or set(provider) - {"kind", "url_env", "token_env", "timeout_s"}:
        raise ValueError("formal_provider configuration is invalid")
    if provider.get("kind") not in {"canonical-http", "mathematics-ai-http"}:
        raise ValueError("production requires a supported real HTTP formal provider")
    url_name = provider.get("url_env")
    endpoint = os.environ.get(url_name, "") if isinstance(url_name, str) else ""
    if not endpoint:
        raise ValueError("configured formal provider URL environment variable is unset")
    token_name = provider.get("token_env")
    token = os.environ.get(token_name) if isinstance(token_name, str) else None
    if token_name and not token:
        raise ValueError("configured formal provider token environment variable is unset")
    timeout = provider.get("timeout_s", 10)
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise ValueError("formal provider timeout must be within (0, 30] seconds")
    from .execution import CanonicalExecutor, MathsAIExecutor
    from .production import PostgresArtifacts, ProductionService
    artifacts = PostgresArtifacts(db_url)
    executor_type = MathsAIExecutor if provider["kind"] == "mathematics-ai-http" else CanonicalExecutor
    executor = executor_type(endpoint, token=token, timeout=timeout)
    return ProductionService(root, workspace=config["workspace"], artifacts=artifacts,
                             executor=executor, snapshot=config.get("snapshot"))
