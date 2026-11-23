"""Durable, bounded user workflows over OmegaAdapter's public SDK path.

These records grant no proof authority. The receiver still admits every action
and supplies all mathematical status. A task never retries mathematical work
merely because a transport, model turn, or communication channel disappeared.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import threading

from persistent_atp_client.models import TERMINAL_LIFECYCLES, ArtifactRef, OperationRef, Preconditions, ProofCycle, RefinementPolicy, Budget, refinement_policy_from_dict
from persistent_atp_client.codecs import to_dict
from persistent_atp_client.errors import ClientError, ReceiverProtocolError, StalePrecondition

from ..specification import parse_json, validate_spec


READ_ONLY_KINDS = frozenset({'proof-status', 'proof-audit', 'proof-export'})
MATHEMATICAL_KINDS = frozenset({'proof-cycle', 'formal-search', 'formal-resume', 'lemma-bridge',
    'statement-align', 'hypothesis-formalize', 'hypothesis-revise', 'parent-refine','bounded-refine'})


def _identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class TaskWorkflow:
    """One bounded transition per tick; run ticks on the bridge worker pool."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.state = adapter.state
        self._locks = {}
        self._locks_guard = threading.Lock()

    def accept(self, source, message_id, request):
        """Trusted channel intake, before acknowledging consumption of a frame.

        source must identify a durable server/session namespace, not a socket
        connection. Reusing a message ID with a changed body is rejected.
        """
        if not isinstance(source, str) or not source or len(source) > 2048:
            raise ValueError("task source must be a bounded channel namespace")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            raise ValueError("task message ID must be a bounded string")
        if not isinstance(request, dict) or set(request) - {
            "spec", "workspace", "max_cycles", "max_research", "timeout_seconds", "reply_prefix",
            "proof_id", "expected_revision", "expected_theorem_hash",
            'refinement_policy','refinement_budget'
        }:
            raise ValueError("invalid persistent_atp task fields")
        spec = validate_spec(request.get("spec"))
        workspace = request.get("workspace", self.adapter.workspace)
        if workspace != self.adapter.workspace:
            raise ValueError("UNREGISTERED_WORKSPACE")
        cycles, research = request.get("max_cycles", 2), request.get("max_research", 1)
        if type(cycles) is not int or not 0 <= cycles <= self.adapter.max_cycles_per_proof:
            raise ValueError("task cycle allowance exceeds configured policy")
        if type(research) is not int or not 0 <= research <= 1:
            raise ValueError("task research allowance must be zero or one")
        timeout = request.get("timeout_seconds", 240)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ValueError("task timeout must be within 1..3600 seconds")
        prefix = request.get("reply_prefix", "PERSISTENT_ATP_FINAL")
        if not isinstance(prefix, str) or not prefix or len(prefix) > 200 or any(c in prefix for c in "\r\n"):
            raise ValueError("reply prefix must be a bounded single line")
        normalized = {"spec": spec, "workspace": workspace, "max_cycles": cycles,
                      "max_research": research, "timeout_seconds": timeout, "reply_prefix": prefix}
        if request.get('refinement_policy') is not None:
            if request['refinement_policy']!='bounded-v1' or not 1<=cycles<=2:
                raise ValueError('bounded-v1 requires one or two parent attempts')
            raw=request.get('refinement_budget')
            policy=(refinement_policy_from_dict(raw) if raw is not None else
                RefinementPolicy(Budget(48,30000),Budget(48,30000),Budget(48,30000),Budget(64,15000),
                    144,128,cycles,research))
            if policy.max_parent_attempts!=cycles or policy.max_research!=research:
                raise ValueError('bounded policy allowances differ from task limits')
            normalized.update(refinement_policy='bounded-v1',refinement_budget=to_dict(policy))
        elif 'refinement_budget' in request:
            raise ValueError('refinement_budget requires explicit bounded-v1 policy')
        proof = request.get("proof_id")
        pins = {"proof_id", "expected_revision", "expected_theorem_hash"}
        if pins & set(request):
            if not pins <= set(request):
                raise ValueError("existing proof requires proof_id, expected_revision and expected_theorem_hash")
            if not {"max_cycles", "max_research"} <= set(request):
                raise ValueError("existing proof task requires explicit max_cycles and max_research")
            ProofCycle(proof)
            revision = request["expected_revision"]
            if type(revision) is not int or revision < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            Preconditions(expected_revision=revision, expected_theorem_hash=request["expected_theorem_hash"])
            if not isinstance(request["expected_theorem_hash"], str):
                raise ValueError("expected_theorem_hash is required")
            normalized.update({key: request[key] for key in pins})
        normalized = parse_json(json.dumps(normalized, ensure_ascii=False, allow_nan=False))
        identity = _identity(source, message_id)
        def save(data):
            existing = data["tasks"].get(identity)
            if existing:
                if existing["request"] != normalized:
                    raise ValueError("task message identity reused with different content")
                return identity
            if identity in data.get("rejected_task_inputs", {}):
                raise ValueError("task message was rejected; use a new message identity")
            now = self.adapter._clock()
            data["tasks"][identity] = {"id": identity, "source": source, "message_id": message_id,
                "request": normalized, "phase": "attach" if proof else "create", "steps": {}, "proof_id": proof,
                "created_at": now, "deadline": now + timeout, "cycles": 0, "research": 0,
                "last_progress_at": now, "errors": [], "reason": None, "delivery": None}
            data["input_mode"] = "managed"
            return identity
        return self.state.update(save)

    def _patch(self, identity, **values):
        return self.state.update(lambda data: data["tasks"][identity].update(values))

    def _step(self, task, name, kind, arguments):
        if arguments.get("proof") and kind not in {"proof-audit", "proof-export", "proof-status"}:
            def own(data):
                owners = data.setdefault("proof_owners", {})
                owner = owners.get(arguments["proof"])
                if owner and owner["task_id"] != task["id"]:
                    raise ValueError("TASK_NO_LONGER_OWNS_PROOF")
                # Migrate active tasks from caches predating proof ownership.
                owners.setdefault(arguments["proof"], {"task_id": task["id"],
                    "claimed_at": self.adapter._clock()})
            self.state.update(own)
        identity = _identity(task["id"], name)
        row = self.state.snapshot()['intents'].get(identity, {})
        if not row.get('ref') and arguments.get('proof') and kind not in READ_ONLY_KINDS:
            blocker = self._blocking_work(arguments['proof'], exclude_request=identity)
            if blocker:
                self._patch(task['id'], **blocker)
                if blocker['reason'] == 'EXISTING_WORK_RUNNING':
                    return None
                return {'error': {'error_code': blocker['reason']}}
            self._patch(task['id'], blocking_operations=[])
        # Reserve by task/step identity even if this operation already completed.
        # A crash between reservation and saving the step cannot duplicate work.
        self.adapter.reserve(kind, self.adapter.workspace, arguments, intent_id=identity,
                             new_intent=True, task_id=task["id"], deadline=datetime.fromtimestamp(
                                 task["finalization_deadline"] if kind in {"proof-audit", "proof-export"}
                                 else task["deadline"], timezone.utc))
        if name not in task["steps"]:
            steps = {**task["steps"], name: identity}
            counts = {"cycles": 1} if name == "cycle-1" else {"research": 1} if name == "research-1" else {}
            self._patch(task["id"], steps=steps, last_progress_at=self.adapter._clock(), **counts)
        row = self.state.intent(identity)
        if row.get("ref"):
            if row.get("observation_suspended"):
                return {"error": row.get("last_error", {"error_code": "OBSERVATION_SUSPENDED"})}
            if row.get("observation_retry_at", 0) > self.adapter._clock():
                return None
            self.adapter.observe(identity)
        else:
            self.adapter.execute(identity)
        row = self.state.intent(identity)
        if row["state"] in {"rejected", "needs-input"} or row.get("retry_exhausted"):
            return {"error": row.get("last_error", {"error_code": "STEP_REQUIRES_RECONCILIATION"})}
        snapshot = row.get("snapshot")
        if kind=='bounded-refine' and snapshot:
            result=snapshot.get('result') or {}
            self._patch(task['id'],cycles=result.get('cycles_used',task['cycles']),
                research=result.get('research_used',task['research']))
        if snapshot and snapshot["lifecycle"] in TERMINAL_LIFECYCLES:
            unsettled = kind not in READ_ONLY_KINDS and self._settlement(snapshot, row) is not True
            if snapshot["lifecycle"] != "succeeded" or unsettled:
                result = snapshot.get('result') or {}
                code = result.get('error_code')
                if not isinstance(code, str) or not code:
                    code = ('EXECUTION_RECONCILIATION_REQUIRED' if unsettled and snapshot['lifecycle'] == 'succeeded'
                        else 'OPERATION_' + snapshot['lifecycle'].upper())
                error = {'error_code': code,
                    'operation_ref': row['ref'], 'request_id': row['id']}
                if isinstance(result.get('summary'), str):
                    error['summary'] = result['summary'][:500]
                return {'error': error, 'result': result, 'snapshot': snapshot}
            return {"result": snapshot.get("result") or {}, "snapshot": snapshot}
        return None

    def _finish_step(self, task, phase, outcome, **values):
        errors = list(task["errors"])
        if outcome and outcome.get("error"):
            errors.append({"step": task["phase"], **outcome["error"]})
        if phase in {"status", "audit", "export", "report"} and not task.get("finalization_deadline"):
            values.setdefault("finalization_deadline", min(self.adapter._clock(), task["deadline"]) + 30)
        self._patch(task["id"], phase=phase, errors=errors,
                    last_progress_at=self.adapter._clock(), **values)

    def _creation_proof(self, task):
        """Resolve only the proof returned by this task's original creation."""
        key = task['steps'].get('create')
        row = self.state.snapshot()['intents'].get(key, {})
        snapshot = row.get('snapshot') or {}
        if not snapshot:
            return None
        if (key != _identity(task['id'], 'create') or row.get('id') != key
                or row.get('kind') != 'proof-create' or row.get('task_id') != task['id']
                or not row.get('request') or not row.get('ref')):
            raise ReceiverProtocolError('creation lacks its original task/request binding')
        request = self.adapter._request(row)
        expected_hash = 'sha256:' + hashlib.sha256(self.adapter._spec_bytes(row)).hexdigest()
        if (request.action.kind != 'proof-create' or row['arguments']['spec'] != task['request']['spec']
                or request.action.spec_artifact.hash != expected_hash
                or snapshot.get('ref') != row['ref'] or snapshot.get('request_id') != key
                or snapshot.get('observation_version', -1) < row['version']):
            raise ReceiverProtocolError('creation observation differs from the original request')
        result = snapshot.get('result')
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise ReceiverProtocolError('creation result must be an object')
        proofs = [value for value in (snapshot.get('proof_id'), result.get('proof_id')) if value is not None]
        try:
            for proof in proofs:
                ProofCycle(proof)  # Validate the identifier without dispatching anything.
        except (ValueError, TypeError) as exc:
            raise ReceiverProtocolError('creation returned an invalid proof identifier') from exc
        if len(set(proofs)) > 1:
            raise ReceiverProtocolError('creation snapshot and result identify different proofs')
        return proofs[0] if proofs else None

    def _recover_creation_proof(self, task):
        """Recover correlation from cached evidence, including after deadlines.

        This never sends a creation or changes an already persisted final reply.
        A recovered proof grants only read-only finalization, not another cycle.
        """
        if task.get('proof_id') or task.get('creation_binding_invalid') or not task['steps'].get('create'):
            return task
        try:
            proof = self._creation_proof(task)
        except (ClientError, ValueError, TypeError, KeyError) as exc:
            row = self.state.intent(task['steps']['create'])
            self._finish_step(task, 'report', {'error': self.adapter.error(exc, row['id'], row.get('ref'))},
                reason='CREATION_REQUIRES_RECONCILIATION', creation_binding_invalid=True)
        else:
            if proof:
                # A draft from an older coordinator can still collect evidence
                # within its original window. The deadline check runs next.
                self._patch(task['id'], proof_id=proof, phase='status' if task['phase'] == 'report' else task['phase'])
        return self.state.snapshot()['tasks'][task['id']]

    def tick(self, identity):
        # Serialize one task's transitions, never hold an adapter-wide lock
        # while a different proof is waiting on network or provider I/O.
        with self._locks_guard:
            lock = self._locks.setdefault(identity, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            try:
                task = self.state.snapshot()["tasks"][identity]
                if task.get("proof_id"):
                    with self.state.coordinate_proof(task["proof_id"]) as acquired:
                        if acquired:
                            self._tick(identity)
                        elif task["phase"] == "attach":
                            self._patch(identity, reason="WAITING_FOR_TASK_OWNER")
                else:
                    self._tick(identity)
            except (ClientError, ValueError, TypeError, KeyError) as exc:
                task = self.state.snapshot()["tasks"][identity]
                phase = {"audit": "export", "export": "report", "create": "report"}.get(task["phase"], "status")
                values = {}
                if task.get('proof_id') and task['phase'] in {'attach', 'cycle', 'research'}:
                    # Failed prerequisite observations establish neither
                    # settlement nor permission for further mathematical work.
                    values['blocking_operations'] = [*task.get('blocking_operations', []), {
                        'proof_id': task['proof_id'], 'lifecycle': 'uncertain', 'execution_settled': None}]
                self._finish_step(task, phase, {"error": self.adapter.error(exc)},
                    reason="WORKFLOW_REQUIRES_RECONCILIATION", **values)
        finally:
            lock.release()

    def _tick(self, identity):
        task = self.state.snapshot()["tasks"][identity]
        phase = task["phase"]
        if phase in {"delivery", "done"}:
            return
        if phase in {'status', 'audit', 'export', 'report'}:
            task = self._recover_creation_proof(task)
            phase = task['phase']
        now = self.adapter._clock()
        if now >= task["deadline"] and phase not in {"status", "audit", "export", "report"}:
            self._patch(identity, phase="status", reason="TASK_DEADLINE_REACHED",
                        finalization_deadline=task["deadline"] + 30)
            return
        if phase in {"status", "audit", "export", "report"} and not task.get("finalization_deadline"):
            # Older task caches may already be in finalization.
            self._patch(identity, finalization_deadline=min(now, task["deadline"]) + 30)
            task = self.state.snapshot()["tasks"][identity]
        if now >= task.get("finalization_deadline", task["deadline"] + 30):
            self._patch(identity, phase="report", reason=task["reason"] or "FINALIZATION_DEADLINE_REACHED")
            phase = "report"
            task = self.state.snapshot()["tasks"][identity]
        proof = task["proof_id"]
        if phase == "attach":
            self._attach(task)
        elif phase == "create":
            outcome = self._step(task, "create", "proof-create", {"spec": json.dumps(task["request"]["spec"], ensure_ascii=False)})
            if outcome is None:
                return
            try:
                proof = self._creation_proof(self.state.snapshot()['tasks'][identity])
            except (ClientError, ValueError, TypeError, KeyError) as exc:
                if outcome.get('error'):
                    task['errors'].append({'step': 'create', **outcome['error']})
                row = self.state.intent(self.state.snapshot()['tasks'][identity]['steps']['create'])
                self._finish_step(task, 'report', {'error': self.adapter.error(exc, row['id'], row.get('ref'))},
                    reason='CREATION_REQUIRES_RECONCILIATION', creation_binding_invalid=True)
                return
            if outcome.get("error") or not proof:
                self._finish_step(task, 'status' if proof else 'report', outcome, proof_id=proof,
                    reason="TASK_DEADLINE_REACHED" if self.adapter._clock() >= task["deadline"]
                    else "CREATION_REQUIRES_RECONCILIATION")
            else:
                # Bind creation and ownership in the same cache transaction.
                def bind(data):
                    data.setdefault("proof_owners", {})[proof] = {"task_id": identity,
                        "claimed_at": self.adapter._clock()}
                    data["tasks"][identity].update(phase="cycle", proof_id=proof,
                        last_progress_at=self.adapter._clock())
                self.state.update(bind)
        elif phase == "cycle":
            if not task["request"]["max_cycles"]:
                self._finish_step(task, "status", None, reason="TASK_CYCLE_LIMIT_REACHED")
                return
            if task['request'].get('refinement_policy')=='bounded-v1':
                if not task.get('bounded_input'):
                    status=self.adapter.status(proof)
                    created=self.state.snapshot()['intents'].get(task['steps'].get('create'),{})
                    expected=task['request'].get('expected_revision',
                        ((created.get('snapshot') or {}).get('result') or {}).get('revision'))
                    target=task['request'].get('expected_theorem_hash',status['target_artifact']['hash'])
                    raw=self.adapter.client.get_artifact(ArtifactRef(**status['target_artifact']),workspace_id=self.adapter.workspace)
                    if validate_spec(parse_json(raw.decode('utf-8')))!=task['request']['spec']:
                        raise StalePrecondition('bounded task committed specification differs from original intent')
                    if status['revision']!=expected or status['target_artifact']['hash']!=target:
                        raise StalePrecondition('bounded task initial proof identity changed')
                    self._patch(identity,bounded_input={'proof':proof,'policy':json.dumps(task['request']['refinement_budget']),
                        'expected_revision':str(expected),'expected_theorem_hash':target})
                    task=self.state.snapshot()['tasks'][identity]
                outcome=self._step(task,'bounded-refinement','bounded-refine',task['bounded_input'])
                if outcome is not None:
                    result=outcome.get('result') or {}
                    self._finish_step(task,'status',outcome,cycle_result=result,
                        reason=(outcome.get('error') or {}).get('error_code') or result.get('reason','BOUNDED_REFINEMENT_FINISHED'),
                        cycles=result.get('cycles_used',task['cycles']),research=result.get('research_used',task['research']))
                return
            # An automatic cycle includes intake, alignment, search and replay.
            # No second identical search is justified solely by exhaustion.
            outcome = self._step(task, "cycle-1", "proof-cycle", {"proof": proof})
            if outcome is None:
                return
            result = outcome.get("result", {})
            contexts = result.get("obstruction_contexts") or []
            obstruction = next((row.get("obstruction_id") for row in contexts
                                if isinstance(row, dict) and isinstance(row.get("obstruction_id"), str)), None)
            reason = (outcome.get("error") or {}).get("error_code") or result.get("error_code") or \
                result.get("mathematical_disposition") or result.get("cycle_disposition") or "CYCLE_FINISHED"
            if outcome.get("error") and self.adapter._clock() >= task["deadline"]:
                reason = "TASK_DEADLINE_REACHED"
            self._finish_step(task, "research" if not outcome.get('error') and obstruction and task["request"]["max_research"] else "status",
                outcome, cycle_result=result, obstruction_id=obstruction, reason=reason)
        elif phase == "research":
            outcome = self._step(task, "research-1", "lemma-bridge", {"proof": proof, "obstruction": task["obstruction_id"]})
            if outcome is not None:
                self._finish_step(task, "status", outcome,
                    research_result=outcome.get("result", {}),
                    reason=(outcome.get('error') or {}).get('error_code') or 'CANDIDATE_REVIEW_REQUIRED')
        elif phase == "status":
            if not proof:
                # Creation may have completed after the task observation deadline.
                row = self.state.snapshot()["intents"].get(task["steps"].get("create"), {})
                if row.get("request") and not row.get("ref"):
                    # Expired requests use lookup only; their original bodies
                    # and deadlines cannot be refreshed by a resumed task.
                    self.adapter.execute(row["id"])
                    row = self.state.intent(row["id"])
                if row.get("ref"):
                    # Observation remains allowed after the dispatch deadline.
                    # Never submit another creation to discover its proof ID.
                    self.adapter.observe(row["id"])
                    row = self.state.intent(row["id"])
                task = self._recover_creation_proof(self.state.snapshot()['tasks'][identity])
                if task.get('creation_binding_invalid'):
                    return
                proof = task.get('proof_id')
                if not proof:
                    self._finish_step(task, "report", None)
                    return
            try:
                status = self.adapter.status(proof)
            except (ClientError, ValueError, TypeError, KeyError) as exc:
                # Keep proof result unknown and record why evidence is incomplete.
                self._finish_step(task, "audit", {"error": self.adapter.error(exc)}, status=None)
            else:
                self._finish_step(task, "audit", None, status=status)
        elif phase in {"audit", "export"}:
            arguments = {"proof": proof}
            if phase == "export":
                arguments["destination"] = "default"
            outcome = self._step(task, phase, "proof-" + phase, arguments)
            if outcome is not None:
                self._finish_step(task, "export" if phase == "audit" else "report", outcome,
                                  **{phase: outcome.get("result", {})})
        elif phase == "report":
            # Refresh once after audit/export so concurrent invalidation is never
            # hidden by a previously captured verified digest.
            status = task.get("status")
            if proof:
                try:
                    status = self.adapter.status(proof)
                except (ClientError, ValueError, TypeError, KeyError) as exc:
                    status = None
                    task["errors"].append({"step": "final-status", **self.adapter.error(exc)})
            task["operations"] = {name: self.state.intent(key).get("ref") for name, key in task["steps"].items()}
            task["operation_states"] = {name: (self.state.intent(key).get("snapshot") or {}).get("lifecycle",
                self.state.intent(key)["state"]) for name, key in task["steps"].items()}
            task['operation_execution'] = {name: self._execution(self.state.intent(key)) for name, key in task['steps'].items()}
            message = self._report(task, status)
            delivery = {"message_id": _identity(identity, "final"), "text": message,
                        "state": "pending", "attempts": 0, "next_attempt_at": 0}
            self._patch(identity, status=status, errors=task["errors"], phase="delivery", delivery=delivery)

    def _attach(self, task):
        """Reconcile before claiming an existing proof; never reset an old run."""
        proof, request = task["proof_id"], task["request"]
        status = self.adapter.status(proof)
        if (status["revision"] != request["expected_revision"]
                or status["target_artifact"]["hash"] != request["expected_theorem_hash"]):
            raise StalePrecondition("existing proof target or revision changed; review a new task intent")
        raw = self.adapter.client.get_artifact(ArtifactRef(**status["target_artifact"]), workspace_id=self.adapter.workspace)
        try:
            committed_spec = validate_spec(parse_json(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("existing proof lacks a complete retrievable theorem specification") from exc
        if committed_spec != request["spec"]:
            raise StalePrecondition("task specification differs from the existing proof's exact specification")

        blocker = self._blocking_work(proof)
        if blocker:
            if blocker['reason'] == 'EXISTING_WORK_RUNNING':
                self._patch(task['id'], **blocker)
            else:
                self._finish_step(task, 'status', None, **blocker)
            return

        def claim(data):
            # Reporting/acknowledging a task never releases its proof. The
            # fresh reconciliation above must also establish settled work.
            ownership = data.setdefault("proof_owners", {}).get(proof)
            owners = [other["id"] for other in data["tasks"].values()
                if other["id"] != task["id"] and other.get("proof_id") == proof
                and other["phase"] not in {"attach", "delivery", "done"}]
            if ownership and ownership["task_id"] != task["id"]:
                owner = data["tasks"].get(ownership["task_id"])
                if owner is None or owner["phase"] not in {"delivery", "done"}:
                    owners.append(ownership["task_id"])
            current = data["tasks"][task["id"]]
            if owners:
                current.update(reason="WAITING_FOR_TASK_OWNER", blocking_tasks=owners)
                return
            data["proof_owners"][proof] = {"task_id": task["id"],
                "claimed_at": self.adapter._clock()}
            current.update(phase="status" if status["formal_verified"] else "cycle",
                reason="TARGET_ALREADY_VERIFIED" if status["formal_verified"] else None,
                attachment={"proof_id": proof, "expected_revision": status["revision"],
                            "expected_theorem_hash": status["target_artifact"]["hash"]},
                blocking_operations=[], blocking_tasks=[], last_progress_at=self.adapter._clock())
        self.state.update(claim)

    def _blocking_work(self, proof, *, exclude_request=None):
        """Fresh receiver evidence, shared by ownership transfer and dispatch.

        The caller holds proof coordination. Exclude only the request being
        recovered/dispatched, so an ambiguous admission can still be looked up.
        """
        operations = self.adapter.client.list_operations(workspace_id=self.adapter.workspace, proof_id=proof, active_only=False)
        operations = [row for row in operations if row.request_id != exclude_request]
        active = [row for row in operations if row.lifecycle not in TERMINAL_LIFECYCLES]
        if active:
            return {'reason': 'EXISTING_WORK_RUNNING', 'blocking_operations': [
                {'operation_ref': row.ref.to_dict(), 'lifecycle': row.lifecycle,
                 'request_id': row.request_id, 'execution_settled': False} for row in active]}
        intents = self.state.snapshot()["intents"]
        known = {row['ref']['operation_id']: row for row in intents.values() if row.get('ref')}
        def unresolved(snapshot, intent=None):
            settled = self._settlement(snapshot, intent)
            if settled is not True:
                return {'reason': 'PROOF_RECONCILIATION_REQUIRED', 'blocking_operations': [{
                    'request_id': snapshot.request_id, 'operation_ref': snapshot.ref.to_dict(),
                    'lifecycle': snapshot.lifecycle, 'execution_settled': settled}]}
        for operation in operations:
            intent = known.get(operation.ref.operation_id)
            if intent and (operation.request_id != intent['id'] or operation.proof_id != proof):
                raise ValueError('existing operation differs from its proof/request binding')
            blocker = unresolved(operation, intent)
            if blocker:
                return blocker

        # Terminal task/receiver status is not evidence that an uncertain
        # external search stopped. Reconcile original mathematical intents too.
        for row in intents.values():
            if (row['id'] == exclude_request or row["kind"] not in MATHEMATICAL_KINDS
                    or (row.get("arguments") or {}).get("proof") != proof):
                continue
            if row.get("request"):
                self.adapter._request(row)  # validate the saved immutable binding
            if row.get("request") and not row.get("ref") and row["state"] != "rejected":
                self.adapter._recover_cached_intent(row, None)  # lookup only
                row = self.state.intent(row["id"])
                if not row.get("ref") and not self.adapter._deadline_elapsed(row):
                    return {'reason': 'PROOF_RECONCILIATION_REQUIRED', 'blocking_operations': [
                        {'request_id': row['id'], 'lifecycle': 'uncertain', 'execution_settled': None}]}
            if row.get("ref"):
                snapshot = self.adapter.client.snapshot(self.adapter.client.attach(OperationRef(**row["ref"])))
                if snapshot.request_id != row["id"] or snapshot.proof_id != proof:
                    raise ValueError("existing operation differs from its proof/request binding")
                if snapshot.lifecycle not in TERMINAL_LIFECYCLES:
                    return {'reason': 'EXISTING_WORK_RUNNING', 'blocking_operations': [{
                        'request_id': row['id'], 'operation_ref': row['ref'],
                        'lifecycle': snapshot.lifecycle, 'execution_settled': False}]}
                blocker = unresolved(snapshot, row)
                if blocker:
                    return blocker

    def _settlement(self, snapshot, intent=None):
        """Execution evidence has identical meaning in reports and admission."""
        if not snapshot:
            return None
        if isinstance(snapshot, dict):
            lifecycle, result = snapshot.get('lifecycle'), snapshot.get('result') or {}
        else:
            lifecycle, result = snapshot.lifecycle, snapshot.result or {}
        if lifecycle not in TERMINAL_LIFECYCLES:
            return False
        if intent and intent['kind'] in READ_ONLY_KINDS:
            return True  # These actions cannot leave mathematical work running.
        if result.get('execution_reconciliation'):
            try:
                if self._search_settled(snapshot, intent):
                    return True
            except (ClientError, ValueError, TypeError, KeyError):
                pass  # An unavailable/invalid receipt never authorizes new work.
        if result.get('execution_settled') is False:
            return False
        code = result.get('error_code')
        if (lifecycle != 'failed' and not (isinstance(code, str) and code.endswith('RECONCILIATION_REQUIRED'))
                and result.get('execution_settled') is True):
            return True
        return None

    def _search_settled(self, snapshot, intent=None):
        """Trust only a receiver-bound, content-addressed resolution receipt."""
        if isinstance(snapshot, dict):
            result, ref = snapshot.get('result') or {}, OperationRef(**snapshot['ref'])
            request_id, proof_id = snapshot['request_id'], snapshot.get('proof_id')
        else:
            result, ref = snapshot.result or {}, snapshot.ref
            request_id, proof_id = snapshot.request_id, snapshot.proof_id
        if result.get("error_code") == "FORMAL_REPLAY_RECONCILIATION_REQUIRED":
            return False
        summary = result.get("execution_reconciliation")
        if not isinstance(summary, dict):
            return False
        if (summary.get("format") != "persistent-atp-search-reconciliation-v1"
                or summary.get("execution_settled") is not True or summary.get("replay_performed") is not False
                or summary.get("operation_ref") != ref.to_dict()
                or summary.get("request_id") != request_id or summary.get("proof_id") != proof_id
                or intent is not None and summary.get("request_fingerprint") != intent.get("fingerprint")):
            return False
        receipt = parse_json(self.adapter.client.get_artifact(ArtifactRef(summary["receipt_artifact"]),
            workspace_id=self.adapter.workspace).decode("utf-8"))
        if not isinstance(receipt, dict) or any(receipt.get(key) != value
                for key, value in summary.items() if key != "receipt_artifact"):
            return False
        return (receipt.get("provider_status") in {"finished", "interrupted"}
            and receipt.get("execution_settled") is True and receipt.get("replay_performed") is False)

    def _execution(self, row):
        snapshot = row.get('snapshot') or {}
        result = snapshot.get('result') or {}
        code = result.get('error_code')
        reconciliation = result.get('execution_reconciliation')
        if not row.get('ref'):
            settled = True if not row.get('request') or row['kind'] in READ_ONLY_KINDS else None
        else:
            settled = self._settlement(snapshot, row)
        return {'request_id': row['id'], 'operation_ref': row.get('ref'),
            'lifecycle': snapshot.get('lifecycle', row['state']), 'observation_version': snapshot.get('observation_version'),
            'execution_settled': settled, 'error_code': code,
            'summary': result['summary'][:500] if isinstance(result.get('summary'), str) else None,
            'reconciliation_receipt': reconciliation.get('receipt_artifact') if isinstance(reconciliation, dict) else None}

    @staticmethod
    def _report(task, status):
        def evidence(value):
            if not value:
                return value
            fields = {key: value[key] for key in ("revision", "event_hash", "audit_status", "export_artifact",
                "formal_verified", "operation_id", "projection_lag", "hypothesis_ids", "hypothesis_status",
                "result_artifact", "packet_artifact", "error_code", "clarification_reasons", "required_work",
                "cycle_disposition", "mathematical_disposition", "formalization_disposition", "replay_status",
                "budget", 'summary', 'run_id', 'claim_id', 'execution_settled', 'execution_reconciliation',
                'lemma_use_audit_artifact', 'refinement_artifact', 'previous_declaration_id', 'declaration_id',
                'bounded_audit_artifact','bounded_usage','cycles_used','research_used','candidate_attempts',
                'selected_hypothesis_id','reason') if key in value}
            fields["rendering_incomplete"] = set(value) - set(fields) != set()
            for key, item in list(fields.items()):
                if len(json.dumps(item, ensure_ascii=False).encode()) > 2048:
                    fields[key] = {"preview": str(item)[:200], "rendering_incomplete": True}
                    fields["rendering_incomplete"] = True
            return fields
        verified = status.get("formal_verified") if status else None
        expected_hash = task["request"].get("expected_theorem_hash")
        if task["request"].get("proof_id") and not task.get("attachment"):
            verified = None  # Intake/reconciliation has not validated this task's exact spec.
        if status and expected_hash and status["target_artifact"]["hash"] != expected_hash:
            verified = None  # A different target must never satisfy this task.
        target = (status or {}).get("literal_target", task["request"]["spec"]["theorem"])
        result = {"task_id": task["id"], "proof_id": task["proof_id"],
            "literal_target": target[:2000], "literal_target_incomplete": len(target) > 2000,
            "target_artifact": (status or {}).get("target_artifact"),
            "formal_verified": verified, "claim_status": (status or {}).get("claim_status"),
            "proof_formal_verified": (status or {}).get("formal_verified"),
            "revision": (status or {}).get("revision"), "event_hash": (status or {}).get("event_hash"),
            "reason": task["reason"], "cycles_used": task["cycles"], "research_used": task["research"],
            "audit": evidence(task.get("audit")), "export": evidence(task.get("export")), "errors": task["errors"],
            "requests": task["steps"], "operations": task.get("operations", {}),
            "operation_states": task.get("operation_states", {}),
            'operation_execution': task.get('operation_execution', {}),
            "cycle": evidence(task.get("cycle_result")),
            "research": evidence(task.get("research_result"))}
        result["attachment"] = task.get("attachment")
        result["blocking_operations"] = task.get("blocking_operations", [])
        result['blocking_tasks'] = task.get('blocking_tasks', [])
        settlements = [entry['execution_settled'] for entry in result['operation_execution'].values()]
        if task.get('creation_binding_invalid'):
            settlements.append(None)
        settlements.extend(entry.get('execution_settled',
            False if entry['lifecycle'] not in TERMINAL_LIFECYCLES | {'uncertain'} else None)
            for entry in result['blocking_operations'])
        if result['blocking_tasks']:
            settlements.append(None)
        result['execution_settled'] = False if False in settlements else None if None in settlements else True
        result['execution_may_continue'] = result['execution_settled'] is not True
        label = "Target verified by receiver" if verified is True else "Target remains unverified" if verified is False else "Target verification status unavailable"
        # Audit/export identities are preserved even if they describe an older
        # revision. Do not claim they certify the latest digest after a race.
        result["evidence_matches_status"] = bool(status and all(
            evidence.get("revision") == status.get("revision") and
            evidence.get("event_hash") == status.get("event_hash")
            for evidence in (task.get("audit") or {}, task.get("export") or {})))
        if result['execution_settled'] is None:
            label += '. Execution settlement is unknown; reconcile the original operations'
        elif result['execution_settled'] is False:
            label += '. Receiver work may still be running'
        return task["request"]["reply_prefix"] + " — " + label + ".\n" + json.dumps(result, ensure_ascii=False, sort_keys=True)

    def pending_delivery(self, source):
        now = self.adapter._clock()
        return [deepcopy(task) for task in self.state.snapshot()["tasks"].values()
                if task["source"] == source and task["phase"] == "delivery"
                and task["delivery"]["next_attempt_at"] <= now
                and task["delivery"]["attempts"] < 4]

    def claim_delivery(self, source):
        """Reserve one attempt immediately before its socket send.

        Persist uncertainty before I/O so a crash cannot turn an ambiguous
        send into a fresh message. Later replies retain their retry allowance.
        """
        now = self.adapter._clock()
        def claim(data):
            for task in data["tasks"].values():
                if task["source"] != source or task["phase"] != "delivery":
                    continue
                delivery = task["delivery"]
                if delivery["next_attempt_at"] > now or delivery["attempts"] >= 4:
                    continue
                delivery["attempts"] += 1
                delivery["state"] = "delivery-uncertain"
                delivery["next_attempt_at"] = now + min(30, 2 ** delivery["attempts"])
                return task
            return None
        return self.state.update(claim)

    def acknowledge_delivery(self, source, message_id):
        def update(data):
            for task in data["tasks"].values():
                if (task["source"] == source and (task.get("delivery") or {}).get("message_id") == message_id
                        and task["delivery"]["attempts"] > 0):
                    task["delivery"]["state"] = "acknowledged"
                    task["phase"] = "done"
                    return True
            return False
        return self.state.update(update)

    def reject(self, source, message_id, reason):
        """Persist intake errors as deliverable outcomes without admitting work."""
        identity = _identity(source, message_id, "rejected", reason)
        def save(data):
            if identity in data["tasks"]:
                return
            original = _identity(source, message_id)
            if original not in data["tasks"]:
                data.setdefault("rejected_task_inputs", {})[original] = reason[:500]
            data["tasks"].setdefault(identity, {"id": identity, "source": source, "message_id": message_id,
                "phase": "delivery", "delivery": {"message_id": _identity(identity, "final"),
                "text": "PERSISTENT_ATP_FINAL — Task rejected: " + reason[:500],
                "state": "pending", "attempts": 0, "next_attempt_at": 0}})
        self.state.update(save)

    def actionable(self):
        return [task["id"] for task in self.state.snapshot()["tasks"].values()
                if task["phase"] not in {"done", "delivery"}]
