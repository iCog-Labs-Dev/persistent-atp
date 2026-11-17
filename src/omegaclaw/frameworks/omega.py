"""Omega coordination over the public SDK, with no mathematical authority."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit

from persistent_atp_client import (
    AsyncClient, ArtifactRef, Budget, FormalResume, FormalSearch, LemmaBridge,
    Preconditions, ProofAudit, ProofCreate, ProofCycle, ProofExport, StatementAlign,
    SyncClient,
)
from persistent_atp_client.models import OperationRef, TERMINAL_LIFECYCLES
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.codecs import fingerprint, request_from_dict, to_dict
from persistent_atp_client.errors import (
    ClientError, CursorExpired, ReceiverProtocolError, SubmissionUncertain,
    UnsupportedCapability,
)

from ..specification import parse_json, validate_spec
from .omega_state import OmegaState

SKILLS = {
    "proof-create": ("spec",), "proof-cycle": ("proof",), "proof-status": ("proof",),
    "proof-audit": ("proof",), "proof-export": ("proof", "destination"),
    "formal-search": ("proof", "claim", "budget"), "formal-resume": ("proof", "run", "budget"),
    "statement-align": ("proof", "claim", "lean_source"), "lemma-bridge": ("proof", "obstruction"),
}


class OmegaAdapter:
    def __init__(self, port, *, workspace, cache_path=None, receiver_id="local", max_cycles_per_proof=10):
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
        return cls(HTTPReceiverAdapter(url, token), workspace=workspace, cache_path=path, receiver_id=url,
                   max_cycles_per_proof=limit)

    def reserve(self, kind, workspace, arguments, *, intent_id=None, new_intent=False):
        """Checkpoint workflow intent without doing network I/O on Omega's loop."""
        if workspace != self.workspace:
            raise ValueError("UNREGISTERED_WORKSPACE")
        if kind not in SKILLS or set(arguments) != set(SKILLS[kind]):
            raise ValueError("INVALID_REQUEST")
        if not all(isinstance(value, str) for value in arguments.values()):
            raise ValueError("tool arguments must be strings")
        parsed = dict(arguments)
        if kind == "proof-create":
            parsed["spec"] = validate_spec(parse_json(arguments["spec"]))
        if "budget" in arguments:
            value = parse_json(arguments["budget"])
            if not isinstance(value, dict) or set(value) != {"max_steps", "wall_clock_ms"}:
                raise ValueError("INVALID_BUDGET")
            parsed["budget"] = to_dict(Budget(**value))
        if kind == "statement-align":
            ArtifactRef(arguments["lean_source"])
        # SDK-generated DTO identifiers also constrain proof/destination inputs.
        if "proof" in arguments:
            ProofCycle(arguments["proof"])
        if kind == "proof-export":
            ProofExport(arguments["proof"], arguments["destination"])
        identity = intent_id or uuid.uuid4().hex
        def reserve(data):
            existing = data["intents"].get(identity)
            if existing:
                if existing["kind"] != kind or existing["arguments"] != parsed:
                    raise ValueError("intent identity was reused with different arguments")
                return existing["id"]
            if not new_intent:
                for row in data["intents"].values():
                    if row["state"] not in {"completed", "rejected"} and row["kind"] == kind:
                        if row["arguments"] == parsed or (kind == "proof-create" and row["arguments"] is None):
                            return row["id"]
            if kind == "proof-cycle":
                count = sum(row["kind"] == kind and row.get("arguments", {}).get("proof") == parsed["proof"]
                            for row in data["intents"].values() if row.get("arguments") is not None)
                if count >= self.max_cycles_per_proof:
                    raise ValueError("CYCLE_LIMIT_REACHED")
            data["intents"][identity] = {"id": identity, "kind": kind, "arguments": parsed,
                "state": "reserved", "request": None, "ref": None, "cursor": None,
                "event_ids": [], "event_sequence": -1, "version": -1}
            return identity
        return self.state.update(reserve)

    def startup(self):
        """Reconcile persisted references and empty-cache active creations."""
        with self._startup_lock:
            self.caps = self.client.capabilities(contract_version="2.0")
            active = self.client.list_operations(workspace_id=self.workspace, active_only=True)
            for snapshot in active:
                identity = snapshot.request_id
                if identity not in self.state.snapshot()["intents"]:
                    self.state.update(lambda data, item=snapshot: data["intents"].setdefault(identity, {
                        "id": identity, "kind": "proof-create" if item.proof_id is None else "recovered",
                        "arguments": None, "state": "accepted", "request": None,
                        "ref": to_dict(item.ref), "cursor": None, "event_ids": [],
                        "event_sequence": -1, "version": -1}))
                else:
                    self.state.patch(identity, ref=to_dict(snapshot.ref), state="accepted")
            for row in self.state.snapshot()["intents"].values():
                if row["state"] in {"completed", "rejected"} or not row.get("request"):
                    continue
                request = self._request(row)
                receipt = self.client.lookup_request(workspace_id=self.workspace, request_id=request.request_id)
                if receipt is not None:
                    if not receipt.accepted or receipt.fingerprint != fingerprint(request):
                        raise ReceiverProtocolError("recovered admission differs from prepared request")
                    self.state.patch(row["id"], ref=to_dict(receipt.operation_ref), state="accepted")
            selected = self.state.snapshot().get("selected_proof")
            if selected:
                self.status(selected)
            self._started = True
            return to_dict(self.caps)

    @staticmethod
    def _request(row):
        request = request_from_dict(row["request"])
        if fingerprint(request) != row["fingerprint"] or request.request_id != row["id"]:
            raise ReceiverProtocolError("workflow checkpoint request identity mismatch")
        return request

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
            raw = json.dumps(args["spec"], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            return ProofCreate(self.client.put_artifact(raw, workspace_id=self.workspace, media_type="application/json")), None
        digest = self.client.status(workspace_id=self.workspace, proof_id=args["proof"])
        pre = Preconditions(expected_revision=digest.revision, expected_theorem_hash=digest.target_artifact.hash)
        proof = args["proof"]
        if kind in {"formal-search", "formal-resume"}:
            if self.caps.backend_mode != "real":
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
        try:
            if not self._started:
                self.startup()
            row = self.state.intent(identity)
            if row.get("ref"):
                return self._admission(row)
            if row["kind"] == "proof-status":
                result = self.status(row["arguments"]["proof"])
                self.state.patch(identity, state="completed")
                return {**result, "request_id": identity}
            if row.get("request"):
                request = self._request(row)
            else:
                action, pre = self._action(row)
                request = self.client.prepare(action, workspace_id=self.workspace, request_id=identity, preconditions=pre)
                self.state.patch(identity, request=to_dict(request), fingerprint=fingerprint(request), state="prepared")
            handle = self.client.submit(request)
            row = self.state.patch(identity, ref=to_dict(handle.ref), state="accepted")
            return self._admission(row)
        except (ClientError, ValueError, TypeError, KeyError) as exc:
            row = self.state.intent(identity)
            uncertain = isinstance(exc, SubmissionUncertain) or (isinstance(exc, ClientError) and exc.retryable)
            # An observation failure never cancels or rejects already admitted work.
            self.state.patch(identity, state="accepted" if row.get("ref") else "uncertain" if uncertain else "rejected")
            return self.error(exc, identity, row.get("ref"))

    def _admission(self, row):
        return {"phase": "admitted", "transport_status": "accepted", "durable_admission": True,
                "request_id": row["id"], "operation_id": row["ref"]["operation_id"],
                "operation_ref": row["ref"], "message": "Operation accepted/queued; mathematical outcome is not established."}

    @staticmethod
    def error(exc, identity=None, ref=None):
        return {"transport_status": "error", "error_code": getattr(exc, "code", "INVALID_REQUEST"),
                "request_id": identity or getattr(exc, "request_id", None), "operation_ref": ref,
                "message": "Reconcile this request; do not infer proof success or resubmit with a new identity."}

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
            try:
                events, cursor = self.client.events_page(ref, cursor=row["cursor"])
            except CursorExpired:
                events, cursor = self.client.events_page(ref, cursor=None)
                refreshed = True
            reports = []
            seen = set(row["event_ids"])
            ordered_ids = list(row["event_ids"])
            fingerprints = dict(row.get("event_fingerprints", {}))
            sequence = row["event_sequence"]
            event_version = row.get("event_version", -1)
            for event in events:
                if event.event_id in seen:
                    if fingerprints.get(event.event_id, fingerprint(event)) != fingerprint(event):
                        raise ReceiverProtocolError("event identity changed across pages")
                    continue
                if event.sequence <= sequence:
                    if refreshed:
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
            if snapshot.observation_version != row["version"] or refreshed:
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
                report = {**to_dict(snapshot), "notification_id": f"{ref.operation_id}:{snapshot.observation_version}:{int(refreshed)}",
                          "operation_id": ref.operation_id, "operation_ref": row["ref"],
                          "request_id": identity, "message": message, "history_incomplete": refreshed,
                          "requested_budget": (row.get("request") or {}).get("action", {}).get("budget"),
                          "effective_budget": outcome.get("budget"), "mathematical_disposition": disposition}
                reports.append(report)
            def save(data):
                item = data["intents"][identity]
                item.update(cursor=cursor if refreshed else cursor or row["cursor"], event_ids=ordered_ids[-2000:],
                            event_fingerprints={key: fingerprints[key] for key in ordered_ids[-2000:] if key in fingerprints},
                            event_sequence=sequence, event_version=event_version, version=snapshot.observation_version,
                            state="completed" if snapshot.lifecycle in TERMINAL_LIFECYCLES else "accepted")
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
        return [row for row in self.state.snapshot()["intents"].values() if row["state"] == "accepted" and row.get("ref")]

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
