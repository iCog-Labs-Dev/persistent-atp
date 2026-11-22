"""Omega coordination over the public SDK, with no mathematical authority."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlsplit

from persistent_atp_client import (
    AsyncClient, ArtifactRef, Budget, FormalResume, FormalSearch, LemmaBridge,
    Preconditions, ProofAudit, ProofCreate, ProofCycle, ProofExport, StatementAlign,
    SyncClient, HypothesisFormalize, HypothesisRevise, ParentRefine,
)
from persistent_atp_client.models import OperationRef, TERMINAL_LIFECYCLES
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.codecs import fingerprint, request_from_dict, to_dict
from persistent_atp_client.errors import (
    AdmissionRejected, ClientError, CursorExpired, ReceiverProtocolError,
    UnsupportedCapability, StalePrecondition, TransientUnavailable,
)

from ..specification import parse_json, validate_spec
from .omega_state import OmegaState

SKILLS = {
    "proof-create": ("spec",), "proof-cycle": ("proof",), "proof-status": ("proof",),
    "proof-audit": ("proof",), "proof-export": ("proof", "destination"),
    "formal-search": ("proof", "claim", "budget"), "formal-resume": ("proof", "run", "budget"),
    "statement-align": ("proof", "claim", "lean_source"), "lemma-bridge": ("proof", "obstruction"),
    "hypothesis-formalize": ("proof", "hypothesis", "expected_revision"),
    "hypothesis-revise": ("proof", "hypothesis", "spec", "previous_claim", "expected_revision"),
    "parent-refine": ("proof", "previous_declaration", "lemma", "budget", "expected_revision", "expected_theorem_hash"),
}


class OmegaAdapter:
    def __init__(self, port, *, workspace, cache_path=None, receiver_id="local", max_cycles_per_proof=10,
                 retry_limit=4, retry_base_seconds=0.5, clock=time.time):
        # SDK validation of the configured scope; this is never model authority.
        OperationRef(workspace, "scope-check")
        self.workspace = workspace
        self.client = SyncClient(AsyncClient(port))
        self.state = OmegaState(cache_path, scope=[receiver_id, workspace])
        self._startup_lock = threading.RLock()
        self._started = False
        self.caps = None
        if type(max_cycles_per_proof) is not int or max_cycles_per_proof <= 0:
            raise ValueError("max_cycles_per_proof must be a positive finite allowance")
        self.max_cycles_per_proof = max_cycles_per_proof
        if type(retry_limit) is not int or not 1 <= retry_limit <= 10:
            raise ValueError("retry_limit must be within 1..10")
        if type(retry_base_seconds) not in (int, float) or not math.isfinite(retry_base_seconds) or retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be finite and positive")
        self.retry_limit, self.retry_base_seconds, self._clock = retry_limit, retry_base_seconds, clock
        from .tasks import TaskWorkflow
        self.tasks = TaskWorkflow(self)

    @classmethod
    def from_environment(cls):
        token = os.environ.get("PERSISTENT_ATP_OMEGA_TOKEN", "")
        if not token:
            raise ValueError("TOKEN_MISSING")
        url = os.environ.get("PERSISTENT_ATP_OMEGA_URL", "http://127.0.0.1:8765")
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("receiver URL must not embed credentials, queries or fragments")
        workspace = os.environ.get("PERSISTENT_ATP_OMEGA_WORKSPACE", "reference")
        path = os.environ.get("PERSISTENT_ATP_OMEGA_STATE", str(Path.home() / ".local/state/persistent-atp/omega.json"))
        limit = int(os.environ.get("PERSISTENT_ATP_OMEGA_MAX_CYCLES", "10"))
        retry_limit = int(os.environ.get("PERSISTENT_ATP_OMEGA_RETRY_LIMIT", "4"))
        retry_base = float(os.environ.get("PERSISTENT_ATP_OMEGA_RETRY_BASE_SECONDS", "0.5"))
        return cls(HTTPReceiverAdapter(url, token), workspace=workspace, cache_path=path, receiver_id=url,
                   max_cycles_per_proof=limit, retry_limit=retry_limit, retry_base_seconds=retry_base)

    def reserve(self, kind, workspace, arguments, *, intent_id=None, new_intent=False, task_id=None, deadline=None):
        """Checkpoint workflow intent without doing network I/O on Omega's loop."""
        if workspace != self.workspace:
            raise ValueError("UNREGISTERED_WORKSPACE")
        if kind not in SKILLS or set(arguments) != set(SKILLS[kind]):
            raise ValueError("INVALID_REQUEST")
        if not all(isinstance(value, str) for value in arguments.values()):
            raise ValueError("tool arguments must be strings")
        parsed = dict(arguments)
        if kind in {"proof-create", "hypothesis-revise"}:
            parsed["spec"] = validate_spec(parse_json(arguments["spec"]))
        if "budget" in arguments:
            value = parse_json(arguments["budget"])
            if not isinstance(value, dict) or set(value) != {"max_steps", "wall_clock_ms"}:
                raise ValueError("INVALID_BUDGET")
            parsed["budget"] = to_dict(Budget(**value))
        if kind == "statement-align":
            ArtifactRef(arguments["lean_source"])
        if kind == 'parent-refine':
            revision = arguments['expected_revision']
            if not revision.isascii() or not revision.isdecimal():
                raise ValueError('expected_revision must be a nonnegative decimal revision')
            parsed['expected_revision'] = str(int(revision))
            ParentRefine(arguments['proof'], arguments['previous_declaration'], arguments['lemma'], Budget(**parsed['budget']))
            Preconditions(expected_revision=int(revision), expected_theorem_hash=arguments['expected_theorem_hash'])
        if kind in {"hypothesis-formalize", "hypothesis-revise"}:
            HypothesisFormalize(arguments['proof'], arguments['hypothesis'])
            if not arguments['hypothesis'].startswith(arguments['proof'] + '/'):
                raise ValueError('hypothesis is outside the requested proof')
            revision = arguments['expected_revision']
            if not revision.isascii() or not revision.isdecimal():
                raise ValueError('expected_revision must be a nonnegative decimal revision')
            parsed['expected_revision'] = str(int(revision))
            if kind == 'hypothesis-revise' and arguments['previous_claim']:
                if not arguments['previous_claim'].startswith(arguments['proof'] + '/'):
                    raise ValueError('previous claim is outside the requested proof')
        # SDK-generated DTO identifiers also constrain proof/destination inputs.
        if "proof" in arguments:
            ProofCycle(arguments["proof"])
        if kind == "proof-export":
            ProofExport(arguments["proof"], arguments["destination"])
        identity = intent_id or uuid.uuid4().hex
        if deadline is not None and (not isinstance(deadline, datetime) or deadline.tzinfo is None
                                     or deadline.utcoffset().total_seconds() != 0):
            raise ValueError("intent deadline must be an aware UTC datetime")
        deadline_value = deadline.isoformat().replace("+00:00", "Z") if deadline is not None else None
        def reserve(data):
            if task_id is None:
                for task in data.get("tasks", {}).values():
                    owns_proof = parsed.get("proof") is not None and parsed.get("proof") == task.get("proof_id")
                    owns_spec = kind == "proof-create" and parsed.get("spec") == (task.get("request") or {}).get("spec")
                    if (owns_proof and kind not in {"proof-status", "proof-audit", "proof-export"}) or owns_spec:
                        raise ValueError("TASK_OWNED_BY_COORDINATOR")
            existing = data["intents"].get(identity)
            if existing:
                if existing["kind"] != kind or existing["arguments"] != parsed:
                    raise ValueError("intent identity was reused with different arguments")
                if deadline_value is not None and existing.get("deadline") != deadline_value:
                    if existing.get("deadline") is not None:
                        raise ValueError("intent identity was reused with a different deadline")
                    if not existing.get("request") and not existing.get("ref"):
                        existing["deadline"] = deadline_value
                    # Prepared legacy requests are immutable. execute() only
                    # looks those up; it will not submit them without a deadline.
                return existing["id"]
            if not new_intent:
                for row in data["intents"].values():
                    if row["state"] not in {"completed", "rejected"} and row["kind"] == kind:
                        if row["arguments"] == parsed or (kind == "proof-create" and row["arguments"] is None):
                            return row["id"]
            if kind in {"proof-cycle", "parent-refine"}:
                count = sum(row["kind"] in {"proof-cycle", "parent-refine"} and row.get("arguments", {}).get("proof") == parsed["proof"]
                            for row in data["intents"].values() if row.get("arguments") is not None)
                if count >= self.max_cycles_per_proof:
                    raise ValueError("CYCLE_LIMIT_REACHED")
            data["intents"][identity] = {"id": identity, "kind": kind, "arguments": parsed,
                "state": "reserved", "request": None, "ref": None, "cursor": None,
                "event_ids": [], "event_sequence": -1, "version": -1, "task_id": task_id,
                "deadline": deadline_value}
            return identity
        return self.state.update(reserve)

    def startup(self):
        """Reconcile persisted references and empty-cache active creations."""
        with self._startup_lock:
            self.caps = self.client.capabilities(contract_version="2.0")
            active = self.client.list_operations(workspace_id=self.workspace, active_only=True)
            active_by_id = {snapshot.request_id: snapshot for snapshot in active}
            for snapshot in active:
                identity = snapshot.request_id
                if identity not in self.state.snapshot()["intents"]:
                    self.state.update(lambda data, item=snapshot: data["intents"].setdefault(identity, {
                        "id": identity, "kind": "proof-create" if item.proof_id is None else "recovered",
                        "arguments": None, "state": "accepted", "request": None,
                        "ref": to_dict(item.ref), "cursor": None, "event_ids": [],
                        "event_sequence": -1, "version": -1}))
            for row in self.state.snapshot()["intents"].values():
                if row["state"] in {"rejected", "needs-input"}:
                    continue
                if row["state"] == "completed" and not row.get("observation_pending", True):
                    continue
                try:
                    self._recover_cached_intent(row, active_by_id.get(row["id"]))
                except (ClientError, ValueError, TypeError, KeyError) as exc:
                    self._recovery_failure(row, exc)
            self._started = True
            selected = self.state.snapshot().get("selected_proof")
            if selected:
                self.refresh_status(selected)
            return to_dict(self.caps)

    def _recover_cached_intent(self, row, active_snapshot):
        request = self._request(row) if row.get("request") else None
        if row.get("ref"):
            if row.get("observation_suspended") or row.get("observation_retry_at", 0) > self._clock():
                return
            # The operation reference remains useful after request-index expiry.
            # Do not depend on lookup retention for an already admitted request.
            ref = OperationRef(**row["ref"])
            if ref.workspace_id != self.workspace:
                raise ReceiverProtocolError("cached operation belongs to another workspace")
            snapshot = active_snapshot or self.client.snapshot(self.client.attach(ref))
            if (snapshot.ref != ref or snapshot.request_id != row["id"]
                    or snapshot.observation_version < row["version"]):
                raise ReceiverProtocolError("recovered operation identity or version differs")
            self.state.patch(row["id"], state="completed" if snapshot.lifecycle in TERMINAL_LIFECYCLES else "accepted",
                             recovery_failures=0)
        elif request is not None:
            receipt = self.client.lookup_request(workspace_id=self.workspace, request_id=request.request_id)
            if receipt is not None:
                if receipt.fingerprint != fingerprint(request):
                    raise ReceiverProtocolError("recovered admission differs from prepared request")
                if not receipt.accepted:
                    raise AdmissionRejected(receipt)
                self.state.patch(row["id"], ref=to_dict(receipt.operation_ref), state="accepted",
                                 observation_pending=True, recovery_failures=0)

    def _recovery_failure(self, row, exc):
        failures = row.get("recovery_failures", 0) + 1
        retryable = isinstance(exc, ClientError) and exc.retryable
        exhausted = row.get("retry_exhausted", False) or (retryable and failures >= self.retry_limit)
        next_attempt = self._clock() + min(30, self.retry_base_seconds * 2 ** min(failures - 1, 6))
        report = {**self.error(exc, row["id"], row.get("ref")),
                  "automatic_retry": retryable and not exhausted, "retry_exhausted": exhausted}
        values = {"recovery_failures": failures, "last_error": report}
        if row.get("ref"):
            values.update(observation_suspended=not retryable or exhausted, observation_retry_at=next_attempt)
        else:
            values.update(state="uncertain" if retryable else "needs-input",
                          next_attempt_at=next_attempt, retry_exhausted=exhausted)
        self.state.patch(row["id"], **values)
        self.stage_report(f"recovery:{row['id']}:{self._error_code(exc)}", report)

    def bootstrap_due(self):
        row = self.state.snapshot().get("bootstrap", {})
        return (not self._started and row.get("attempts", 0) < self.retry_limit
                and row.get("next_attempt_at", 0) <= self._clock() and not row.get("stopped"))

    def bootstrap(self):
        """Recover an empty cache after temporary startup failure, with a cap."""
        with self._startup_lock:
            if not self.bootstrap_due():
                return
            row = self.state.snapshot().get("bootstrap", {})
            attempts = row.get("attempts", 0) + 1
            try:
                self.startup()
            except (ClientError, ValueError, TypeError, KeyError) as exc:
                retryable = isinstance(exc, ClientError) and exc.retryable
                self.state.update(lambda data: data.update(bootstrap={"attempts": attempts,
                    "stopped": not retryable, "next_attempt_at": self._clock() + min(30,
                        self.retry_base_seconds * 2 ** (attempts - 1))}))
                report = {**self.error(exc), "automatic_retry": retryable and attempts < self.retry_limit,
                          "retry_exhausted": retryable and attempts >= self.retry_limit}
                self.stage_report(f"startup:{self._error_code(exc)}:{attempts}", report)

    def _request(self, row):
        request = request_from_dict(row["request"])
        if (fingerprint(request) != row["fingerprint"] or request.request_id != row["id"]
                or request.workspace_id != self.workspace):
            raise ReceiverProtocolError("workflow checkpoint request identity mismatch")
        if row.get("deadline") is not None and to_dict(request)["deadline"] != row["deadline"]:
            raise ReceiverProtocolError("workflow checkpoint deadline differs from prepared request")
        return request

    def _deadline_elapsed(self, row):
        return row.get("deadline") is not None and self._clock() >= datetime.fromisoformat(
            row["deadline"].replace("Z", "+00:00")).timestamp()

    def _deadline_reconcile(self, row):
        """Read-only recovery; never resubmit an expired or legacy task intent."""
        if row.get("request"):
            self._recover_cached_intent(row, None)
            recovered = self.state.intent(row["id"])
            if recovered.get("ref"):
                return self._admission(recovered)
        raise StalePrecondition("task request expired or lacks its bound deadline; no new admission permitted")

    def _check_identity(self, proof, identity, label):
        cursor = None
        for _ in range(100):
            page = self.client.coordination_page(proof, workspace_id=self.workspace, cursor=cursor)
            if any(item.item_type == "node" and item.record_id == identity and item.label == label for item in page.items):
                return
            cursor = page.next_cursor
            if cursor is None:
                raise ValueError("selected identity is absent from authoritative proof state")
        raise UnsupportedCapability("identity lookup requires more coordination pages")

    def _action(self, row):
        kind, args = row["kind"], row["arguments"]
        if kind == "proof-create":
            raw = self._spec_bytes(row)
            return ProofCreate(self.client.put_artifact(raw, workspace_id=self.workspace, media_type="application/json")), None
        digest = self.client.status(workspace_id=self.workspace, proof_id=args["proof"])
        pre = Preconditions(expected_revision=digest.revision, expected_theorem_hash=digest.target_artifact.hash)
        proof = args["proof"]
        if kind == 'parent-refine':
            if (digest.revision != int(args['expected_revision'])
                    or digest.target_artifact.hash != args['expected_theorem_hash']):
                raise StalePrecondition('parent refinement pins changed; prepare a new explicit intent')
            caps = self.client.capabilities(contract_version='2.3')
            if kind not in caps.actions:
                raise UnsupportedCapability('parent refinement is unavailable')
            self._check_identity(proof, args['previous_declaration'], 'FormalDeclaration')
            self._check_identity(proof, args['lemma'], 'Claim')
            return ParentRefine(proof, args['previous_declaration'], args['lemma'], Budget(**args['budget'])), \
                Preconditions(expected_revision=int(args['expected_revision']),
                    expected_theorem_hash=args['expected_theorem_hash'])
        if row.get("task_id"):
            task = self.state.snapshot()["tasks"][row["task_id"]]
            pins = task["request"]
            if pins.get("proof_id") and kind not in {"proof-audit", "proof-export"}:
                if digest.target_artifact.hash != pins["expected_theorem_hash"]:
                    raise StalePrecondition("attached proof target changed")
                if kind == "proof-cycle" and digest.revision != pins["expected_revision"]:
                    raise StalePrecondition("attached proof changed before dispatch; review a new task intent")
        if kind in {'hypothesis-formalize', 'hypothesis-revise'}:
            expected = int(args['expected_revision'])
            if digest.revision != expected:
                raise StalePrecondition('selected hypothesis revision changed; review before starting a new intent')
            caps = self.client.capabilities(contract_version='2.2')
            if kind not in caps.actions:
                raise UnsupportedCapability('hypothesis formalization is unavailable')
            self._check_identity(proof, args['hypothesis'], 'SpeculativeHypothesis')
            action = HypothesisFormalize(proof, args['hypothesis'])
            if kind == 'hypothesis-revise':
                if args['previous_claim']:
                    self._check_identity(proof, args['previous_claim'], 'Claim')
                ref = self.client.put_artifact(self._spec_bytes(row), workspace_id=self.workspace,
                    media_type='application/json')
                action = HypothesisRevise(proof, args['hypothesis'], ref, args['previous_claim'] or None)
            return action, Preconditions(
                expected_revision=expected, expected_theorem_hash=digest.target_artifact.hash)
        if kind in {"formal-search", "formal-resume"}:
            caps = self.client.capabilities(contract_version='2.0')
            if caps.backend_mode == 'unavailable' and kind in caps.actions and 'reconcile-search' in caps.controls:
                raise TransientUnavailable('formal provider readiness is pending')
            if caps.backend_mode != "real":
                raise UnsupportedCapability("FORMAL_BACKEND_UNAVAILABLE")
            if kind == "formal-resume" and "durable-resume" not in self.caps.controls:
                raise UnsupportedCapability("durable checkpoint continuation is unavailable")
        if kind in {"formal-search", "statement-align"}:
            self._check_identity(proof, args["claim"], "Claim")
        if kind == "formal-resume":
            self._check_identity(proof, args["run"], "FormalRun")
        if kind == "lemma-bridge":
            self._check_identity(proof, args["obstruction"], "Obstruction")
        if kind == "proof-cycle": return ProofCycle(proof), pre
        if kind == "proof-audit": return ProofAudit(proof), pre
        if kind == "proof-export": return ProofExport(proof, args["destination"]), pre
        if kind == "formal-search": return FormalSearch(proof, args["claim"], Budget(**args["budget"])), pre
        if kind == "formal-resume": return FormalResume(proof, args["run"], Budget(**args["budget"])), pre
        if kind == "statement-align":
            ref = ArtifactRef(args["lean_source"])
            self.client.get_artifact(ref, workspace_id=self.workspace)
            return StatementAlign(proof, args["claim"], ref), pre
        if kind == "lemma-bridge": return LemmaBridge(proof, args["obstruction"]), pre
        raise ValueError("unsupported mutation")

    def execute(self, identity):
        """Submit once through the SDK; never wait for execution completion."""
        row = self.state.intent(identity)
        proof = (row.get("arguments") or {}).get("proof")
        if proof and row['kind'] not in {'proof-status', 'proof-audit', 'proof-export'}:
            with self.state.coordinate_proof(proof) as acquired:
                if not acquired:
                    return {'request_id': identity, 'phase': 'waiting', 'transport_status': 'pending',
                            'error_code': 'PROOF_COORDINATION_BUSY', 'automatic_retry': True}
                return self._execute(identity)
        return self._execute(identity)

    def _execute(self, identity):
        row = self.state.intent(identity)
        if row.get("ref"):
            return self._admission(row)
        if row["state"] in {"rejected", "needs-input"} or row.get("retry_exhausted"):
            return row.get("last_error", {"request_id": identity, "transport_status": "error"})
        if row.get("attempts", 0) >= self.retry_limit:
            report = {**row.get("last_error", self.error(ValueError(), identity)),
                      "automatic_retry": False, "retry_exhausted": True}
            self.state.patch(identity, retry_exhausted=True, last_error=report)
            return report
        if row.get("next_attempt_at", 0) > self._clock():
            return row["last_error"]
        self.state.patch(identity, attempts=row.get("attempts", 0) + 1)
        try:
            with self._startup_lock:
                if not self._started:
                    self.startup()
            row = self.state.intent(identity)
            if row.get("ref"):
                return self._admission(row)
            if (row["state"] in {"rejected", "needs-input"} or row.get("retry_exhausted")
                    or row.get("next_attempt_at", 0) > self._clock()):
                return row["last_error"]
            if row["kind"] == "proof-status":
                result = self.status(row["arguments"]["proof"])
                self.state.patch(identity, state="completed")
                return {**result, "request_id": identity}
            if row["kind"] not in {"proof-audit", "proof-export"}:
                data = self.state.snapshot()
                proof = (row.get("arguments") or {}).get("proof")
                if proof:
                    owner = data.get("proof_owners", {}).get(proof)
                    managed = any(task.get("proof_id") == proof for task in data["tasks"].values())
                    if (managed and not row.get("task_id") or
                            owner and owner["task_id"] != row.get("task_id")):
                        raise ValueError("TASK_OWNED_BY_COORDINATOR")
            legacy_task = row.get("task_id") and row.get("deadline") is None
            if self._deadline_elapsed(row) or legacy_task:
                return self._deadline_reconcile(row)
            if row.get("request"):
                request = self._request(row)
            else:
                action, pre = self._action(row)
                request = self.client.prepare(action, workspace_id=self.workspace, request_id=identity, preconditions=pre,
                    deadline=datetime.fromisoformat(row["deadline"].replace("Z", "+00:00")) if row.get("deadline") else None)
                self.state.patch(identity, request=to_dict(request), fingerprint=fingerprint(request), state="prepared")
            if row.get("artifact_repair_pending"):
                self.client.put_artifact(self._spec_bytes(row), workspace_id=self.workspace,
                    media_type="application/json", expected_hash=request.action.spec_artifact.hash)
            if self._deadline_elapsed(row):
                return self._deadline_reconcile(self.state.intent(identity))
            handle = self.client.reconcile(request) if row.get("request") else self.client.submit(request)
            row = self.state.patch(identity, ref=to_dict(handle.ref), state="accepted",
                                   observation_pending=True, next_attempt_at=0, artifact_repair_pending=False)
            return self._admission(row)
        except (ClientError, ValueError, TypeError, KeyError) as exc:
            row = self.state.intent(identity)
            code = self._error_code(exc)
            retryable = code in {"SUBMISSION_UNCERTAIN", "TRANSIENT_UNAVAILABLE", "RATE_LIMITED"}
            repair = code == "HASH_MISMATCH" and row["kind"] in {"proof-create", "hypothesis-revise"}
            report = self.error(exc, identity, row.get("ref"))
            if code == "STALE_PRECONDITION" and (row.get("arguments") or {}).get("proof"):
                try:
                    report["current_status"] = self.status(row["arguments"]["proof"])
                except ClientError as refresh_error:
                    report["refresh_error_code"] = self._error_code(refresh_error)
            attempts = row.get("attempts", 1)
            exhausted = (retryable or repair) and attempts >= self.retry_limit
            next_attempt = self._clock() + min(30, self.retry_base_seconds * 2 ** (attempts - 1))
            report.update(automatic_retry=(retryable or repair) and not exhausted,
                          retry_exhausted=exhausted)
            self.state.patch(identity,
                state="uncertain" if retryable or repair else "needs-input" if code in {
                    "STALE_PRECONDITION", "HASH_MISMATCH", "ARTIFACT_UNAVAILABLE", "REVIEW_REQUIRED"} else "rejected",
                next_attempt_at=next_attempt, retry_exhausted=exhausted, last_error=report,
                artifact_repair_pending=repair)
            return report

    @staticmethod
    def _spec_bytes(row):
        return json.dumps(row["arguments"]["spec"], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()

    def _admission(self, row):
        return {"phase": "admitted", "transport_status": "accepted", "durable_admission": True,
                "request_id": row["id"], "operation_id": row["ref"]["operation_id"],
                "operation_ref": row["ref"], "message": "Operation accepted/queued; mathematical outcome is not established."}

    @staticmethod
    def _error_code(exc):
        return (exc.admission.error_code or exc.code) if isinstance(exc, AdmissionRejected) else getattr(exc, "code", "INVALID_REQUEST")

    @staticmethod
    def error(exc, identity=None, ref=None):
        code = OmegaAdapter._error_code(exc)
        action, message = {
            "STALE_PRECONDITION": ("refresh-and-review", "State changed. Review the refreshed target/revision before authorizing a new request."),
            "HASH_MISMATCH": ("repair-artifact", "Artifact bytes did not match. Repair the original artifact; do not substitute another theorem or source."),
            "ARTIFACT_UNAVAILABLE": ("authorized-upload-required", "Supply the original artifact through the trusted upload path."),
            "SUBMISSION_UNCERTAIN": ("lookup-original-request", "Admission is uncertain. Reconcile the original request before resubmission."),
            "TRANSIENT_UNAVAILABLE": ("bounded-retry", "Receiver temporarily unavailable; retain the request identity."),
            "RATE_LIMITED": ("bounded-retry", "Receiver rate limit reached; wait before retrying the original request."),
            "FORBIDDEN": ("stop", "Receiver denied this action. Do not retry or change identity to bypass authorization."),
            "IDEMPOTENCY_CONFLICT": ("stop", "Request identity conflicts with the admitted body. Stop and reconcile."),
            "REQUEST_ID_EXPIRED": ("stop-and-review", "Request lookup expired. Review the operation/history before authorizing further work."),
            "UNSUPPORTED_CAPABILITY": ("stop", "Receiver capability is unavailable; no fallback execution was started."),
        }.get(code, ("stop-and-review", "Reconcile this request; no mathematical outcome is inferred."))
        return {"transport_status": "error", "error_code": code, "recovery_action": action,
                "request_id": identity or getattr(exc, "request_id", None), "operation_ref": ref,
                "message": message}

    def status(self, proof):
        digest = self.client.status(workspace_id=self.workspace, proof_id=proof)
        def save(data):
            previous = data["proofs"].get(proof)
            if previous and digest.revision < previous["revision"]:
                raise ReceiverProtocolError("receiver proof revision regressed")
            invalidated = previous and previous["formal_verified"] and not digest.formal_verified
            result = {**to_dict(digest), "event_hash": digest.journal_hash,
                      "transport_status": "ok", "milestone": "verification-invalidated" if invalidated else "status",
                      "message": "Previous verification is invalidated." if invalidated else
                      "Receiver status for the literal target; audit completeness is not inferred."}
            if digest.projection_watermark < digest.revision:
                result["message"] += " Committed evidence exists; graph view is recovering."
            data["proofs"][proof] = to_dict(digest)
            data["selected_proof"] = proof
            if invalidated:
                key = f"invalidation:{proof}:{digest.revision}:{digest.journal_hash}"
                if key not in data.get("displayed", []):
                    data.setdefault("outbox", {}).setdefault(key, {**result, "notification_id": key})
            return result
        return self.state.update(save)

    def observe(self, identity):
        """One bounded observation pass, with durable cursor and notification outbox."""
        row = self.state.intent(identity)
        if not row.get("ref"):
            return []
        ref = OperationRef(**row["ref"])
        try:
            snapshot = self.client.snapshot(self.client.attach(ref))
            if snapshot.request_id != identity or snapshot.observation_version < row["version"]:
                raise ReceiverProtocolError("operation identity or observation version regressed")
            refreshed = False
            replay_until = row.get("replay_until_sequence")
            replay_sequence = row.get("replay_sequence", -1)
            cursor_epoch = row.get("cursor_epoch", 0)
            history_incomplete = row.get("history_incomplete", False)
            try:
                events, cursor = self.client.events_page(ref, cursor=row["cursor"])
            except CursorExpired:
                events, cursor = self.client.events_page(ref, cursor=None)
                refreshed = True
                replay_until = row["event_sequence"]
                replay_sequence = -1
                cursor_epoch += 1
                history_incomplete = True
            reports = []
            seen = set(row["event_ids"])
            ordered_ids = list(row["event_ids"])
            fingerprints = dict(row.get("event_fingerprints", {}))
            sequence = row["event_sequence"]
            event_version = row.get("event_version", -1)
            for event in events:
                if replay_until is not None:
                    replay_sequence = max(replay_sequence, event.sequence)
                if event.event_id in seen:
                    if fingerprints.get(event.event_id, fingerprint(event)) != fingerprint(event):
                        raise ReceiverProtocolError("event identity changed across pages")
                    continue
                if event.sequence <= sequence:
                    if replay_until is not None and event.sequence <= replay_until:
                        continue  # Expired history outside our bounded deduplication window.
                    raise ReceiverProtocolError("event sequence regressed across pages")
                if event.observation_version < event_version:
                    raise ReceiverProtocolError("event observation version regressed")
                sequence = event.sequence
                event_version = event.observation_version
                seen.add(event.event_id)
                ordered_ids.append(event.event_id)
                fingerprints[event.event_id] = fingerprint(event)
                reports.append({"notification_id": event.event_id, "event": to_dict(event),
                                "request_id": identity, "operation_ref": row["ref"]})
            # The replay watermark survives subsequent pages and restarts.
            # Retained duplicate IDs still undergo fingerprint validation above.
            if replay_until is not None and (not events or replay_sequence >= replay_until):
                replay_until = None
            terminal = snapshot.lifecycle in TERMINAL_LIFECYCLES
            # A terminal snapshot does not imply its event stream was drained.
            # One empty page after the terminal snapshot confirms catch-up.
            caught_up = not events and not refreshed and replay_until is None
            drained = terminal and caught_up
            history_incomplete = history_incomplete or (drained and event_version < snapshot.observation_version)
            if snapshot.observation_version != row["version"] or refreshed or drained != (row.get("observation_pending") is False):
                outcome = dict(snapshot.result or {})
                disposition = snapshot.mathematical_disposition or outcome.get("cycle_disposition")
                message = "Operation lifecycle changed; this is not proof completion."
                if disposition == "stagnated": message = "Search returned a valid stagnation result; target remains open."
                if "candidate" in str(disposition) or outcome.get("certificate_status") in {"candidate", "replay-pending"}:
                    message = "Candidate proof awaiting required independent replay/review."
                if outcome.get("claim_status") == "critic-accepted":
                    message = "Claim passed a research review; formal verification is not implied."
                if outcome.get("audit_recommended") or outcome.get("cycle_disposition") in {"idle", "blocked", "waiting-on-review"}:
                    message = "No free eligible work or waiting for review; this is not proof completion."
                report = {**to_dict(snapshot), "notification_id": f"{ref.operation_id}:{snapshot.observation_version}:{int(refreshed)}:{int(drained)}:{cursor_epoch}",
                          "operation_id": ref.operation_id, "operation_ref": row["ref"],
                          "request_id": identity, "message": message,
                          "history_incomplete": history_incomplete,
                          "event_history_complete": drained and not history_incomplete,
                          "event_stream_caught_up": caught_up, "events_pending": not drained,
                          "requested_budget": (row.get("request") or {}).get("action", {}).get("budget"),
                          "effective_budget": outcome.get("budget"), "mathematical_disposition": disposition}
                reports.append(report)
            def save(data):
                item = data["intents"][identity]
                item.update(cursor=cursor if refreshed else cursor or row["cursor"], event_ids=ordered_ids[-2000:],
                            event_fingerprints={key: fingerprints[key] for key in ordered_ids[-2000:] if key in fingerprints},
                            event_sequence=sequence, event_version=event_version, version=snapshot.observation_version,
                            observation_pending=not drained, observation_failures=0, observation_retry_at=0,
                            replay_until_sequence=replay_until, replay_sequence=replay_sequence,
                            cursor_epoch=cursor_epoch, history_incomplete=history_incomplete,
                            state="completed" if snapshot.lifecycle in TERMINAL_LIFECYCLES else "accepted",
                            snapshot=to_dict(snapshot))
                outbox = data.setdefault("outbox", {})
                for report in reports:
                    if report["notification_id"] not in data.get("displayed", []):
                        outbox.setdefault(report["notification_id"], report)
            self.state.update(save)
            if snapshot.proof_id:
                status = self.status(snapshot.proof_id)
                key = f"status:{snapshot.proof_id}:{status['revision']}"
                self.stage_report(key, status)
            return reports
        except (ClientError, ValueError, TypeError, KeyError) as exc:
            failures = row.get("observation_failures", 0) + 1
            self.state.patch(identity, observation_failures=failures,
                observation_retry_at=self._clock() + min(30, self.retry_base_seconds * 2 ** min(failures - 1, 6)),
                observation_suspended=not (isinstance(exc, ClientError) and exc.retryable))
            return [self.error(exc, identity, row["ref"])]

    def stage_report(self, identity, report):
        def stage(data):
            if identity not in data.get("displayed", []):
                data.setdefault("outbox", {}).setdefault(identity, {**report, "notification_id": identity})
        self.state.update(stage)

    def pending_reports(self):
        return list(self.state.snapshot().get("outbox", {}).values())

    def acknowledge(self, identities):
        def ack(data):
            for identity in identities:
                data.setdefault("outbox", {}).pop(identity, None)
            data["displayed"] = list(dict.fromkeys([*data.get("displayed", []), *identities]))[-2000:]
        self.state.update(ack)

    def active_intents(self):
        return [row for row in self.state.snapshot()["intents"].values()
                if row.get("ref") and row.get("observation_pending", True)
                and not row.get("observation_suspended") and row.get("observation_retry_at", 0) <= self._clock()]

    def dispatchable_intents(self):
        return [row for row in self.state.snapshot()["intents"].values()
                if row["state"] in {"reserved", "prepared", "uncertain"} and not row.get("ref")
                and row.get("arguments") is not None and not row.get("retry_exhausted")
                and row.get("attempts", 0) < self.retry_limit
                and row.get("next_attempt_at", 0) <= self._clock()]

    def resume_reconciliation(self, identity):
        """Trusted host action after reviewing exhausted transient delivery.

        Not a model skill. Permanent/semantic errors cannot be overridden here.
        """
        row = self.state.intent(identity)
        if row["state"] != "uncertain" or not row.get("retry_exhausted"):
            raise ValueError("only exhausted transient delivery can be resumed")
        if row.get("request"):
            self._request(row)
        self.state.patch(identity, attempts=0, next_attempt_at=0, retry_exhausted=False)
        return identity

    def refresh_status(self, proof):
        """Background status observation with backoff and permanent-error stop."""
        row = self.state.snapshot().get("status_observation", {}).get(proof, {})
        if row.get("suspended") or row.get("next_attempt_at", 0) > self._clock():
            return
        try:
            status = self.status(proof)
            # Projection recovery can change the digest without a new commit.
            key = f"status:{proof}:{status['revision']}:{status['projection_watermark']}:{int(status['formal_verified'])}"
            self.stage_report(key, status)
            value = {"failures": 0, "next_attempt_at": 0}
        except (ClientError, ValueError, TypeError, KeyError) as exc:
            failures = row.get("failures", 0) + 1
            value = {"failures": failures, "suspended": not (isinstance(exc, ClientError) and exc.retryable),
                     "next_attempt_at": self._clock() + min(30, self.retry_base_seconds * 2 ** min(failures - 1, 6))}
            self.stage_report(f"status-error:{proof}:{self._error_code(exc)}", {**self.error(exc), "proof_id": proof})
        self.state.update(lambda data: data.setdefault("status_observation", {}).update({proof: value}))

    def control(self, name, value):
        """Trusted UI path only; controls are never registered as model skills."""
        if not self._started:
            self.startup()
        if name not in {"cancel", "amend_budget", "answer_decision"}:
            raise ValueError("unknown control")
        capability = {"cancel": "cancel", "amend_budget": "amend-budget", "answer_decision": "answer-decision"}[name]
        if capability not in self.caps.controls:
            raise UnsupportedCapability(f"receiver does not advertise {capability}")
        return to_dict(getattr(self.client, name)(value))

    def artifact_excerpt(self, ref, *, max_chars=8000):
        """Bounded authorized rendering; omitted obligations remain retrievable."""
        if type(max_chars) is not int or not 1 <= max_chars <= 32000:
            raise ValueError("max_chars must be within 1..32000")
        data = self.client.get_artifact(ref, workspace_id=self.workspace)
        result = {"artifact_ref": to_dict(ref), "size_bytes": len(data)}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {**result, "rendering_incomplete": True, "message": "Binary artifact; use authorized download."}
        result.update(text=text[:max_chars], rendering_incomplete=len(text) > max_chars)
        if len(text) <= 65536:
            try:
                payload = parse_json(text)
                if isinstance(payload, dict):
                    result["packet_version"] = payload.get("packet_version", payload.get("schema_version"))
            except ValueError:
                pass
        return result

    def close(self):
        self.client.close()
