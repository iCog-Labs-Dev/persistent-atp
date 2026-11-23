"""One explicit new parent attempt; this does not resume a native frontier.

The operational ledger records intent before fenced gate writes. Earlier
declarations, claims, source bytes and run evidence remain unchanged.
"""
from copy import deepcopy
from pathlib import Path
import json
import time

from commit_gate.canon import canonical_json, content_hash
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from mathproof.formal_v2 import MANIFEST_HASH

from .lemma_reuse import _artifact, _node, verified_lemma, validate_binding
from .formal_runtime import ReplayReconciliationRequired


class ParentRefinementError(ValueError):
    code = 'PARENT_REFINEMENT_REQUIRES_REVIEW'

    def __init__(self, message):
        super().__init__(message)
        self.public_summary = message


class ParentRefinementWorkflow:
    def __init__(self, service):
        self.service = service

    def _save(self, operation, state):
        self.service.jobs.save_refinement(operation, state)

    def validate_history(self, operation):
        """Only prepared refinement/intake writes may advance this attempt."""
        service = self.service
        state = service.jobs.refinement_state(operation)
        if not state or state.get('contract_manifest') != MANIFEST_HASH:
            raise ValueError('parent refinement requires its original durable binding')
        validate_binding(service, state['lemma'])
        for record in state['records']:
            node = service.view.node(record['id'])
            if node is None or node.label != record['label'] or dict(node.fields) != record['fields']:
                raise ValueError('original parent refinement inputs changed')
        prepared = state.get('commit_proposals', []) + service.jobs.intake_state(operation).get('commit_proposals', [])
        if any(event not in prepared for _, event in
               service.store.read_events_after(state['proof_id'], state['expected_revision'])):
            raise ValueError('proof changed outside the parent refinement operation')
        return state

    def _prepare(self, operation, request, deadline):
        service = self.service
        proof, previous, lemma_id = (request[key] for key in
            ('proof_id', 'previous_declaration_id', 'lemma_claim_id'))
        if service.intake.config.lemma_reuse == 'disabled':
            raise ValueError('parent refinement requires enabled verified lemma reuse')
        binary = Path(service.formal.config.reuse_lean or '')
        if not binary.is_absolute() or not binary.is_file():
            raise ValueError('parent refinement requires an installed native Lean auditor')
        if service.store.head(proof)[0] != request['expected_revision']:
            raise ValueError('proof changed before parent refinement admission')
        parent = _node(service, proof + '/c-1', 'Claim')
        if parent.fields.get('status') not in {'provisional', 'critic-accepted'}:
            raise ValueError('parent refinement requires an unverified active parent')
        target = _node(service, proof + '/proof', 'Proof')
        if target.fields.get('target_artifact') != request['expected_theorem_hash']:
            raise ValueError('literal parent target changed before refinement')
        declaration = _node(service, previous, 'FormalDeclaration')
        if declaration.fields.get('status') not in {'aligned', 'searching'}:
            raise ValueError('previous parent declaration is inactive')
        associated = {e.dst_id for e in service.view.edges_from(previous, 'FORMALIZES')}
        associated.update(e.src_id for e in service.view.edges_to(previous, 'FORMALIZES'))
        for edge in service.view.edges_to(previous, 'ALIGNS_DECLARATION'):
            associated.update(e.dst_id for e in service.view.edges_from(edge.src_id, 'ALIGNS_CLAIM'))
        if associated != {parent.node_id} or declaration.fields.get('claim_id') not in {None, parent.node_id}:
            raise ValueError('previous declaration must formalize exactly the literal parent')
        runs = sorted({e.src_id for e in service.view.edges_to(previous, 'SEARCHES')})
        if not runs:
            raise ValueError('parent refinement requires a previous unsuccessful search')
        records = [parent, target, declaration]
        for identity in runs:
            run = _node(service, identity, 'FormalRun')
            result = json.loads(_artifact(service, run.fields.get('provider_result_artifact')))
            binding = json.loads(_artifact(service, run.fields.get('provider_binding_artifact')))
            original = binding.get('request') or {}
            if (result.get('disposition') not in {'stagnated', 'budget-exhausted'}
                    or run.fields.get('status') != result['disposition']
                    or (binding.get('snapshot') or {}).get('result') != result
                    or original.get('proof_id') != proof or original.get('claim_id') != parent.node_id
                    or original.get('formal_declaration_id') != previous
                    or original.get('input', {}).get('artifact_hash') != declaration.fields.get('lean_source_artifact')
                    or original.get('input', {}).get('declaration') != declaration.fields.get('lean_name')
                    or original.get('environment_hash') != result.get('environment_hash')
                    or run.fields.get('environment_hash') != result.get('environment_hash')
                    or result.get('run_id') != identity):
                raise ValueError('previous parent search is not settled unsuccessful bound evidence')
            records.append(run)
        lemma = verified_lemma(service, proof, lemma_id)
        pins = service.view.edges_from(previous, 'PINNED_ENVIRONMENT')
        if len(pins) != 1:
            raise ValueError('previous parent requires one environment pin')
        environment = _node(service, pins[0].dst_id, 'Environment')
        if environment.fields.get('environment_hash') != lemma['environment_hash']:
            raise ValueError('previous parent and verified lemma environments differ')
        if any(node.fields.get('environment_hash') != lemma['environment_hash'] for node in records[3:]):
            raise ValueError('previous parent search environment differs from the verified lemma')
        records.append(environment)
        dependencies = {e.dst_id for e in service.view.edges_from(parent.node_id, 'DEPENDS_ON')}
        if dependencies - {lemma_id}:
            raise ValueError('parent refinement supports exactly one verified dependency')
        if service.store.active_leases(proof):
            raise ValueError('parent refinement conflicts with active proof work')
        for row in service.jobs.conn.execute('SELECT * FROM operations'):
            if row['operation_id'] == operation:
                continue
            from .bounded_ledger import BoundedLedger
            scoped=BoundedLedger(service.jobs)
            if row['operation_id']==scoped.owner(proof) and scoped.child_owner(operation)==row['operation_id']:
                continue
            formal = json.loads(row['formal_json']) if row['formal_json'] else {}
            if formal.get('proof_id') == proof and formal.get('phase') != 'completed':
                if formal.get('phase') == 'replay-started' and 'receipt' not in formal:
                    raise ReplayReconciliationRequired('earlier replay requires receipt reconciliation')
                result = (formal.get('binding', {}).get('snapshot') or {}).get('result') or {}
                committed = service.view.node(formal.get('run_id'))
                if (result.get('disposition') not in {'stagnated', 'budget-exhausted', 'invalid-request', 'cancelled', 'counterexample'}
                        or committed is None or not committed.fields.get('provider_result_artifact')):
                    raise ValueError('parent refinement requires reconciliation of earlier formal execution')
            states = [json.loads(row[key]) for key in
                ('response', 'formal_json', 'cycle_json', 'intake_json', 'refinement_json') if row[key]]
            if row['phase'] not in {'finished', 'failed'} and any(item.get('proof_id') == proof for item in states):
                raise ValueError('parent refinement requires settled previous operations')
        lineage = {'format': 'persistent-atp-parent-refinement-v1', 'operation_id': operation,
            'request': request, 'previous_runs': runs, 'verified_lemma': lemma}
        ref = service.artifacts.put(canonical_json(lineage))
        fields = {'artifact_hash': ref, 'media_type': 'application/json', 'format': lineage['format']}
        ops = [UpsertNode('Artifact', ref, fields),
            AddEdge('HAS_ARTIFACT', parent.node_id, ref, proof + '/refinement-' + content_hash(operation)[7:])]
        if not dependencies:
            ops.append(AddEdge('DEPENDS_ON', parent.node_id, lemma_id,
                proof + '/refinement-dependency-' + content_hash(operation)[7:]))
        lease = service.intake._lease(proof, parent.node_id, 'coordinator', conflicts=(previous, lemma_id))
        state = {'contract_manifest': MANIFEST_HASH, 'request': request, 'proof_id': proof,
            'claim_id': parent.node_id, 'expected_revision': request['expected_revision'], 'phase': 'prepared',
            'deadline': deadline, 'lemma': lemma, 'lineage_artifact': ref, 'initial_lease': lease,
            'initial_ops': [op.to_dict() for op in ops],
            'config_hash': service.intake.config.checkpoint_hash(), 'provider_url': service.formal.config.url,
            'formal_config_hash': content_hash(service.formal.config.__dict__),
            'records': [{'id': node.node_id, 'label': node.label, 'fields': deepcopy(dict(node.fields))}
                        for node in records]}
        return state, ops

    def run(self, operation, proof, previous, lemma, budget, *, expected_revision, expected_theorem_hash,
            deadline=None, cancelled=lambda: False, on_progress=None, replay_budget=None):
        try:
            return self._run(operation, proof, previous, lemma, budget, expected_revision=expected_revision,
                expected_theorem_hash=expected_theorem_hash, deadline=deadline, cancelled=cancelled,
                on_progress=on_progress,replay_budget=replay_budget)
        except ValueError as error:
            raise ParentRefinementError(str(error)) from error
        finally:
            import sys
            if sys.exc_info()[0] is None or issubclass(sys.exc_info()[0], Exception):
                with self.service.lock:
                    state = self.service.jobs.refinement_state(operation)
                    if state.get('initial_lease'):
                        self.service.intake._release(state['initial_lease'])

    def _run(self, operation, proof, previous, lemma, budget, *, expected_revision, expected_theorem_hash,
             deadline, cancelled, on_progress, replay_budget):
        service = self.service
        request = {'proof_id': proof, 'previous_declaration_id': previous, 'lemma_claim_id': lemma,
            'budget': budget, 'expected_revision': expected_revision, 'expected_theorem_hash': expected_theorem_hash}
        if replay_budget:request['replay_budget']=replay_budget
        with service.lock:
            service.catch_up(proof)
            state = service.jobs.refinement_state(operation)
            if state and (state['request'] != request or state['config_hash'] != service.intake.config.checkpoint_hash()
                    or state['formal_config_hash'] != content_hash(service.formal.config.__dict__)
                    or state['provider_url'] != service.formal.config.url
                    or deadline is not None and state['deadline'] != deadline):
                raise ValueError('parent refinement identity or configuration changed')
            if state.get('outcome'):
                return state['outcome']
            if (cancelled() or state and time.time() >= state['deadline']
                    or deadline is not None and time.time() >= deadline):
                return {'proof_id': proof, 'lifecycle': 'cancelled', 'component_mode': 'parent-refinement'}
            if not state:
                limit = deadline if deadline is not None else time.time() + 2 * service.intake.config.timeout + \
                    service.formal.config.wall_clock_ms / 1000 + service.formal.config.replay_wall_clock_ms / 1000 + 120
                state, _ = self._prepare(operation, request, limit)
                self._save(operation, state)  # BEFORE any graph mutation.
        stopped = lambda: cancelled() or time.time() >= state['deadline']
        if not service.jobs.cycle_state(operation):
            with service.lock:
                if stopped():
                    return {'proof_id': proof, 'lifecycle': 'cancelled', 'component_mode': 'parent-refinement'}
                self.validate_history(operation)
                if service.view.node(state['lineage_artifact']) is None:
                    from commit_gate.ops import op_from_dict
                    ops = tuple(op_from_dict(item) for item in state['initial_ops'])
                    saved = state['initial_lease']
                    active = service.store.active_leases(proof)
                    if not any(row.to_dict() == saved for row in active):
                        if active or service.store.head(proof)[0] != saved['base_revision']:
                            raise ValueError('proof changed before refinement lease recovery')
                        state['initial_lease'] = service.intake._lease(proof, state['claim_id'],
                            'coordinator', conflicts=(previous, lemma))
                        self._save(operation, state)
                    lease = state['initial_lease']
                    proposal = Proposal(proof, lease['actor'], lease['worker_class'], ops=ops,
                        base_revision=lease['base_revision'], lease_id=lease['lease_id'],
                        fencing_token=lease['fencing_token'], selected_move_id=lease['selected_move_id'])
                    state.setdefault('commit_proposals', []).append(proposal.to_dict())
                    self._save(operation, state)
                    result = service.gate.commit(proposal)
                    if not result.accepted:
                        raise ValueError('gate rejected parent refinement admission')
                    service.catch_up(proof)
                service.intake._release(state['initial_lease'])
                state['phase'] = 'intake'
                self._save(operation, state)
            prepared = service.intake.run(operation, proof, refinement=True, cancelled=stopped, on_progress=on_progress)
            if not prepared['ready']:
                result = {**prepared, 'proof_id': proof, 'lifecycle': 'cancelled' if stopped() else 'succeeded',
                    'component_mode': 'parent-refinement'}
                with service.lock:
                    state.update(phase='completed', outcome=result)
                    self._save(operation, state)
                return result
            with service.lock:
                self.validate_history(operation)
                state.update(phase='search-ready', declaration_id=prepared['declaration_id'], revision=prepared['revision'])
                self._save(operation, state)
        result = service.cycles.run(operation, proof, declaration_id=state['declaration_id'], budget=budget,
            expected_revision=state['revision'], cancelled=stopped, on_progress=on_progress,replay_budget=replay_budget)
        result = {**result, 'refinement_artifact': state['lineage_artifact'],
            'previous_declaration_id': previous, 'declaration_id': state['declaration_id']}
        with service.lock:
            state.update(phase='completed', outcome=result)
            self._save(operation, state)
        return result
