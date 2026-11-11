"""Durable Omega preparation survives lost delivery acknowledgments."""
import json
import sqlite3

import pytest

from persistent_atp_client import AsyncClient, ProofAudit, SyncClient
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import SubmissionUncertain
from persistent_atp_client.testing import ScriptedReceiverPort
from persistent_atp_client.codecs import canonical_json
from omegaclaw.omega_plugin.omega_intents import IntentStore


class LostAcknowledgment(ScriptedReceiverPort):
    def __init__(self):
        super().__init__()
        self.delivery_uncertain = True
        self.bodies = []

    async def submit(self, request):
        self.bodies.append(canonical_json(request))
        admission = await super().submit(request)
        if self.delivery_uncertain:
            raise TimeoutError("acknowledgment lost")
        if self._operations[admission.operation_ref].lifecycle == "queued":
            self.finish(admission.operation_ref)
        return admission

    async def lookup_request(self, workspace_id, request_id):
        if self.delivery_uncertain:
            raise TimeoutError("lookup unavailable")
        return await super().lookup_request(workspace_id, request_id)


def test_prepared_identity_survives_reopening_after_ambiguous_delivery(tmp_path):
    port = LostAcknowledgment()
    client = SyncClient(AsyncClient(LocalReceiverAdapter(port)))
    path = tmp_path / "intents.sqlite3"
    prepare = lambda: client.prepare(ProofAudit("p1"), workspace_id="reference")
    first = IntentStore(str(path), "receiver-and-principal")
    try:
        with pytest.raises(SubmissionUncertain):
            first.run(client, workspace="reference", intent_id="turn-1-call-1",
                      arguments={"kind": "proof-audit", "proof": "p1"}, prepare=prepare)
    finally: first.close()
    port.delivery_uncertain = False
    reopened = IntentStore(str(path), "receiver-and-principal")
    try:
        request, result = reopened.run(client, workspace="reference", intent_id="turn-1-call-1",
            arguments={"kind": "proof-audit", "proof": "p1"}, prepare=prepare)
        assert result.lifecycle == "succeeded"
        assert len(port._admissions) == 1
        assert port.bodies == [canonical_json(request), canonical_json(request)]
        again, observed = reopened.run(client, workspace="reference", intent_id="turn-1-call-1",
            arguments={"kind": "proof-audit", "proof": "p1"}, prepare=lambda: pytest.fail("must restore"))
        assert again == request and observed.ref == result.ref
        assert len(port.bodies) == 2  # Reattachment reads the authoritative receiver.
        with pytest.raises(ValueError, match="different arguments"):
            reopened.run(client, workspace="reference", intent_id="turn-1-call-1",
                arguments={"kind": "proof-audit", "proof": "p2"}, prepare=prepare)
    finally: reopened.close()


def test_new_intent_starts_a_deliberate_new_operation(tmp_path):
    port = LostAcknowledgment()
    port.delivery_uncertain = False
    client = SyncClient(AsyncClient(LocalReceiverAdapter(port)))
    store = IntentStore(str(tmp_path / "intents.sqlite3"), "receiver")
    try:
        results = [store.run(client, workspace="reference", intent_id=intent,
            arguments={"kind": "proof-audit", "proof": "p1"},
            prepare=lambda: client.prepare(ProofAudit("p1"), workspace_id="reference"))
            for intent in ("turn-1", "turn-2")]
        assert results[0][0].request_id != results[1][0].request_id
        assert results[0][1].ref != results[1][1].ref
    finally: store.close()
