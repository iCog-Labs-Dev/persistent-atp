"""Bind admitted failures to their theorem and reuse the obstruction compiler.

This creates auditable research input, not a research verdict or a verified
lemma. Coordinator leases own the kernel association and packet publication;
the producing formal worker's authority is unchanged.
"""
from types import SimpleNamespace

from artifacts.hashing import hash_content
from commit_gate.canon import canonical_json
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import LeaseCandidate
from context_compiler.obstruction_runtime import ObstructionLeaseCompiler, ObstructionRuntimeConfig
from mathproof.formal_v2_admission import admit_obstruction_binding, _edge
from mathproof.maths_ai_http import MathsAIAPIError as FormalV2Error
from mathproof.scheduler import GlobalScheduler


def prepare_obstruction_context(workflow, operation, state):
    """Finish an admitted stagnation result, recovering saved packet/lease identity."""
    service, proof, run = workflow.service, state['proof_id'], state['run_id']
    with service.lock:
        nodes = sorted(edge.dst_id for edge in service.view.edges_from(run, 'RAISED_OBSTRUCTION'))
        if not nodes:
            raise FormalV2Error('stagnated run has no committed obstruction')
        dispatches = state.setdefault('obstruction_dispatches', {})
        for obstruction in nodes:
            entry = dispatches.setdefault(obstruction, {})
            links = service.view.edges_to(obstruction, 'RESOLVES')
            raised = service.view.edges_to(obstruction, 'RAISED_OBSTRUCTION')
            run_environments = service.view.edges_from(raised[0].src_id, 'RAN_UNDER') if len(raised) == 1 else ()
            needs_binding = len(links) != 1 or len(run_environments) != 1
            if not needs_binding and {edge.src_id for edge in links} != {state['claim_id']}:
                raise FormalV2Error('obstruction is associated with another claim')
            if needs_binding:
                if 'binding_lease' not in entry:
                    # Binding is a prerequisite to frontier eligibility. Claim
                    # this specific free record without superseding any owner.
                    lease = service.store.claim_next(proof, [LeaseCandidate(obstruction, {})],
                        actor='omega-obstruction-coordinator', worker_class='coordinator',
                        base_revision=service.store.head(proof)[0], ttl_seconds=120)
                    if lease is None:
                        raise FormalV2Error('obstruction binding is already leased')
                    entry['binding_lease'] = lease.to_dict()
                    workflow._save(operation, state)
                lease = SimpleNamespace(**entry['binding_lease'])
                try:
                    workflow._accepted(admit_obstruction_binding(lease, service.gate, service.artifacts))
                    service.catch_up(proof)
                finally:
                    workflow._release(lease)
                state['phase'] = 'obstruction-bound'
                workflow._save(operation, state)
            elif {edge.src_id for edge in links} != {state['claim_id']}:
                raise FormalV2Error('obstruction is associated with another claim')
            if 'binding_lease' in entry:
                workflow._release(SimpleNamespace(**entry['binding_lease']))
            if 'packet_lease' not in entry:
                lease = GlobalScheduler(service.view, service.store).lease_selected(proof, obstruction,
                    'coordinator', actor='omega-obstruction-coordinator', ttl_seconds=120)
                if lease is None:
                    raise FormalV2Error('committed obstruction is not free and schedulable')
                entry['packet_lease'] = lease.to_dict()
                workflow._save(operation, state)
            lease = SimpleNamespace(**entry['packet_lease'])
            try:
                if 'packet_artifact' not in entry:
                    dispatch = ObstructionLeaseCompiler(service.view, service.store, service.artifacts,
                        ObstructionRuntimeConfig(max_tokens=8192)).compile(lease)
                    entry.update(packet_artifact=service.artifacts.put(dispatch.packet.content.encode('utf-8')),
                        manifest_artifact=service.artifacts.put(canonical_json(dispatch.manifest)),
                        payload_artifact=service.artifacts.put(canonical_json(dispatch.worker_input)))
                    workflow._save(operation, state)  # BEFORE publishing the saved packet.
                packet = entry['packet_artifact']
                for field in ('packet_artifact', 'manifest_artifact', 'payload_artifact'):
                    if hash_content(service.artifacts.get(entry[field])) != entry[field]:
                        raise FormalV2Error('stored obstruction packet bytes differ from their address')
                fields = {'artifact_hash': packet, 'report_artifact': entry['manifest_artifact'],
                    'result_artifact': entry['payload_artifact'], 'media_type': 'text/plain; charset=utf-8'}
                linked = any(edge.dst_id == packet for edge in service.view.edges_from(obstruction, 'HAS_ARTIFACT'))
                if not linked:
                    workflow._accepted(service.gate.commit(Proposal(proof, lease.actor, lease.worker_class,
                        ops=(UpsertNode('Artifact', packet, fields), _edge(proof, 'HAS_ARTIFACT', obstruction, packet)),
                        base_revision=lease.base_revision, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
                        selected_move_id=lease.selected_move_id)))
                    service.catch_up(proof)
                elif any(service.view.node(packet).fields.get(key) != value for key, value in fields.items()):
                    raise FormalV2Error('published obstruction packet differs from saved dispatch')
            finally:
                workflow._release(lease)
        state['phase'] = 'obstruction-ready'
        workflow._save(operation, state)
        return [{'obstruction_id': key, **{field: value for field, value in entry.items() if field.endswith('_artifact')}}
            for key, entry in sorted(dispatches.items())]
