"""Omega-side, nonauthoritative HTTP bridge. No proof-store imports."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid


def _submit(operation: str, workspace: str, **arguments: str) -> str:
    endpoint = os.environ.get("PERSISTENT_ATP_OMEGA_URL", "http://127.0.0.1:8765").rstrip("/")
    token = os.environ.get("PERSISTENT_ATP_OMEGA_TOKEN", "")
    if not token:
        return json.dumps({"transport_status": "error", "error_code": "TOKEN_MISSING"})
    body = json.dumps({"schema_version": 1, "request_id": uuid.uuid4().hex,
        "workspace": workspace, "operation": operation, "arguments": arguments},
        sort_keys=True, separators=(",", ":")).encode()
    request = urllib.request.Request(endpoint + "/v1/operations", data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read(65536).decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            return exc.read(65536).decode("utf-8")
        except Exception:
            return json.dumps({"transport_status": "error", "error_code": "HTTP_ERROR", "status": exc.code})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return json.dumps({"transport_status": "error", "error_code": "API_UNAVAILABLE", "summary": type(exc).__name__})


def proof_create(spec, workspace): return _submit("proof-create", workspace, spec=spec)
def proof_cycle(proof, workspace): return _submit("proof-cycle", workspace, proof=proof)
def proof_status(proof, workspace): return _submit("proof-status", workspace, proof=proof)
def proof_audit(proof, workspace): return _submit("proof-audit", workspace, proof=proof)
def proof_export(proof, destination, workspace): return _submit("proof-export", workspace, proof=proof, destination=destination)
def formal_search(proof, claim, budget, workspace): return _submit("formal-search", workspace, proof=proof, claim=claim, budget=budget)
def formal_resume(proof, run, budget, workspace): return _submit("formal-resume", workspace, proof=proof, run=run, budget=budget)
def statement_align(proof, claim, lean_source, workspace): return _submit("statement-align", workspace, proof=proof, claim=claim, lean_source=lean_source)
def lemma_bridge(proof, obstruction, workspace): return _submit("lemma-bridge", workspace, proof=proof, obstruction=obstruction)
