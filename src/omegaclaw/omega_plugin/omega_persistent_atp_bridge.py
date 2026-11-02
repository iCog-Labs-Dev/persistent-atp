"""Omega string-tool adapter over the public persistent-atp-client contract."""
from __future__ import annotations

import json
import os
import threading
import urllib.request  # Existing fault-injection tests patch this module.

from persistent_atp_client import (
    AsyncClient, ArtifactRef, Budget, FormalResume, FormalSearch, LemmaBridge,
    ProofAudit, ProofCreate, ProofCycle, ProofExport, StatementAlign, SyncClient,
    SelectedProofCycle, QueueResearchMoves, ResearchGuidance, Preconditions,
)
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.errors import ClientError


_lock = threading.RLock()
_pending = {}  # Nonauthoritative identity cache for uncertain in-process retries.


def _client():
    token = os.environ.get("PERSISTENT_ATP_OMEGA_TOKEN", "")
    if not token:
        raise ValueError("TOKEN_MISSING")
    endpoint = os.environ.get("PERSISTENT_ATP_OMEGA_URL", "http://127.0.0.1:8765")
    return SyncClient(AsyncClient(HTTPReceiverAdapter(endpoint, token)))


def _budget(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"max_steps", "wall_clock_ms"}:
        raise ValueError("INVALID_BUDGET")
    return Budget(**value)


def _invoke(kind: str, workspace: str, **arguments: str) -> str:
    try:
        if any(not isinstance(value, str) for value in (kind, workspace, *arguments.values())):
            raise ValueError("tool arguments must be strings")
        client = _client()
        if kind == "proof-status":
            digest = client.status(workspace_id=workspace, proof_id=arguments["proof"])
            return json.dumps({"proof_id": digest.proof_id, "revision": digest.revision,
                "event_hash": digest.journal_hash, "claim_status": digest.claim_status,
                "formal_verified": digest.formal_verified,
                "literal_target": digest.literal_target,
                "artifact_pointers": [digest.target_artifact.hash],
                "summary": digest.summary, "component_mode": digest.component_mode,
                "transport_status": "ok", "contract_version": "2.0"}, ensure_ascii=False)
        if kind == "coordination-page":
            from persistent_atp_client.codecs import to_dict
            page = client.coordination_page(arguments["proof"], workspace_id=workspace,
                cursor=arguments["cursor"] or None, page_size=int(arguments["page_size"]))
            return json.dumps({**to_dict(page), "transport_status": "ok",
                "contract_version": "2.1"}, ensure_ascii=False)
        if kind == "proof-create":
            spec = json.loads(arguments["spec"])
            if not isinstance(spec, dict) or set(spec) != {"proof_id", "literal_target", "title"}:
                raise ValueError("INVALID_SPEC")
            data = json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ref = client.put_artifact(data, workspace_id=workspace, media_type="application/json")
            action = ProofCreate(ref)
        elif kind == "proof-cycle": action = ProofCycle(arguments["proof"])
        elif kind == "proof-cycle-selected": action = SelectedProofCycle(arguments["proof"], arguments["category"])
        elif kind == "queue-research-moves":
            raw = json.loads(arguments["moves"])
            if not isinstance(raw, list):
                raise ValueError("INVALID_GUIDANCE")
            moves = tuple(ResearchGuidance(**item) for item in raw)
            action = QueueResearchMoves(arguments["proof"], arguments["intent_key"], moves)
        elif kind == "proof-audit": action = ProofAudit(arguments["proof"])
        elif kind == "proof-export": action = ProofExport(arguments["proof"], arguments["destination"])
        elif kind == "formal-search": action = FormalSearch(arguments["proof"], arguments["claim"], _budget(arguments["budget"]))
        elif kind == "formal-resume": action = FormalResume(arguments["proof"], arguments["run"], _budget(arguments["budget"]))
        elif kind == "statement-align": action = StatementAlign(arguments["proof"], arguments["claim"], ArtifactRef(arguments["lean_source"]))
        elif kind == "lemma-bridge": action = LemmaBridge(arguments["proof"], arguments["obstruction"])
        else: raise ValueError("UNKNOWN_ACTION")
        if kind.startswith("formal-") and client.capabilities().backend_mode != "real":
            raise ValueError("FORMAL_BACKEND_UNAVAILABLE")
        key = json.dumps([kind, workspace, arguments], sort_keys=True, ensure_ascii=False)
        with _lock:
            request = _pending.get(key)
            if request is None:
                preconditions = (Preconditions(expected_revision=int(arguments["expected_revision"]))
                    if kind == "queue-research-moves" else None)
                request = client.prepare(action, workspace_id=workspace, preconditions=preconditions)
                _pending[key] = request
        handle = client.submit(request)
        snapshot = client.wait(handle, timeout=10)
        with _lock:
            _pending.pop(key, None)
        outcome = dict(snapshot.result or {})
        outcome.update({"operation_id": snapshot.ref.operation_id, "request_id": request.request_id,
                        "contract_version": request.contract_version,
                        "receiver_mode": client.capabilities(contract_version=request.contract_version).receiver_mode})
        return json.dumps(outcome, sort_keys=True, ensure_ascii=False)
    except ClientError as exc:
        code = "API_UNAVAILABLE" if exc.code in {"TRANSIENT_UNAVAILABLE", "SUBMISSION_UNCERTAIN"} else exc.code
        return json.dumps({"transport_status": "error", "error_code": code,
                           "request_id": exc.request_id}, sort_keys=True)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        code = str(exc) if str(exc) in {"TOKEN_MISSING", "INVALID_BUDGET", "INVALID_SPEC", "INVALID_GUIDANCE", "FORMAL_BACKEND_UNAVAILABLE", "UNKNOWN_ACTION"} else "INVALID_REQUEST"
        return json.dumps({"transport_status": "error", "error_code": code}, sort_keys=True)


def proof_create(spec, workspace): return _invoke("proof-create", workspace, spec=spec)
def proof_cycle(proof, workspace): return _invoke("proof-cycle", workspace, proof=proof)
def proof_status(proof, workspace): return _invoke("proof-status", workspace, proof=proof)
def proof_audit(proof, workspace): return _invoke("proof-audit", workspace, proof=proof)
def proof_export(proof, destination, workspace): return _invoke("proof-export", workspace, proof=proof, destination=destination)
def formal_search(proof, claim, budget, workspace): return _invoke("formal-search", workspace, proof=proof, claim=claim, budget=budget)
def formal_resume(proof, run, budget, workspace): return _invoke("formal-resume", workspace, proof=proof, run=run, budget=budget)
def statement_align(proof, claim, lean_source, workspace): return _invoke("statement-align", workspace, proof=proof, claim=claim, lean_source=lean_source)
def lemma_bridge(proof, obstruction, workspace): return _invoke("lemma-bridge", workspace, proof=proof, obstruction=obstruction)
def coordination_page(proof, cursor, page_size, workspace): return _invoke("coordination-page", workspace, proof=proof, cursor=cursor, page_size=page_size)
def proof_cycle_selected(proof, category, workspace): return _invoke("proof-cycle-selected", workspace, proof=proof, category=category)
def queue_research_moves(proof, intent_key, moves, expected_revision, workspace): return _invoke("queue-research-moves", workspace, proof=proof, intent_key=intent_key, moves=moves, expected_revision=expected_revision)
