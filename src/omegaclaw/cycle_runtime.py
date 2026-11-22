"""One receiver-owned scheduled formal move, using the existing provider workflow.

Selection is operational metadata. Graph preparation, search evidence, replay,
and promotion still use fenced gate proposals. No backend search is implemented
here, and a searching run from another operation is never silently restarted.
"""
from types import SimpleNamespace

from .formal_runtime import ContractMigrationRequired
from mathproof.formal_v2 import MANIFEST_HASH
from mathproof.scheduler import GlobalScheduler


class CycleWorkflow:
    def __init__(self, service):
        self.service = service

    def _save(self, operation, state):
        with self.service.lock:
            self.service.jobs.save_cycle(operation, state)

    def _blocked(self, proof, disposition, **fields):
        return {'proof_id': proof, 'phase': 'cycle-finished', 'lifecycle': 'succeeded',
            'cycle_disposition': disposition, 'component_mode': 'formal-cycle', **fields}

    def run(self, operation, proof, *, category=None, cancelled=lambda: False,
            expected_revision=None, on_progress=None, declaration_id=None, budget=None):
        service = self.service
        if category not in {None, 'formal-run'}:
            raise ValueError('only formal-run has a configured production cycle executor')
        if declaration_id is not None:
            with service.lock:
                refinement = service.jobs.refinement_state(operation)
                if (refinement.get('declaration_id') != declaration_id or refinement.get('proof_id') != proof
                        or refinement['request']['budget'] != budget):
                    raise ValueError('explicit parent selection requires its saved refinement binding')
        if service.intake is not None and declaration_id is None:
            with service.lock:
                saved = service.jobs.cycle_state(operation)
                intake = service.jobs.intake_state(operation)
                service.catch_up(proof)
                revision = service.store.head(proof)[0]
                if not saved and not intake and expected_revision is not None and revision != expected_revision:
                    raise ValueError('proof changed before intake; review the original intent')
                needs_intake = not saved and (bool(intake) or (revision > 0
                    and not service._status(proof)['formal_verified']
                    and not any(item.category == 'formal-run' for item in GlobalScheduler(service.view, service.store).build_frontier(proof))))
            if needs_intake:
                prepared = service.intake.run(operation, proof, cancelled=cancelled, on_progress=on_progress)
                if not prepared['ready']:
                    outcome = self._blocked(proof, 'cancelled' if prepared.get('cancelled') else 'waiting-on-review', **prepared)
                    if prepared.get('cancelled'):
                        outcome['lifecycle'] = 'cancelled'
                    self._save(operation, {'contract_manifest': MANIFEST_HASH, 'proof_id': proof,
                        'category': category, 'phase': 'finished', 'outcome': outcome})
                    return outcome
                expected_revision = prepared['revision']
        with service.lock:
            state = service.jobs.cycle_state(operation)
            if state:
                if state.get('contract_manifest') != MANIFEST_HASH:
                    raise ContractMigrationRequired()
                if (state['proof_id'] != proof or state.get('category') != category
                        or state.get('requested_declaration_id') != declaration_id
                        or declaration_id is not None and state['budget'] != budget):
                    raise ValueError('cycle identity differs from its saved selection')
                if state.get('outcome'):
                    return state['outcome']
            else:
                service.catch_up(proof)
                revision = service.store.head(proof)[0]
                if revision == 0:
                    raise ValueError('unknown proof')
                if expected_revision is not None and revision != expected_revision:
                    raise ValueError('proof changed before cycle selection; review the original intent')
                if cancelled():
                    return {**self._blocked(proof, 'cancelled'), 'lifecycle': 'cancelled'}
                scheduler = GlobalScheduler(service.view, service.store,
                    worker_actors={'formal-atp': 'omega-formal-atp'})
                ttl = service.formal.config.wall_clock_ms / 1000 + 300
                lease = (scheduler.lease_selected(proof, declaration_id, 'formal-atp',
                    actor='omega-formal-atp', ttl_seconds=ttl) if declaration_id is not None else
                    scheduler.lease_best_global_move(proof, category=category, ttl_seconds=ttl))
                state = {'contract_manifest': MANIFEST_HASH, 'proof_id': proof, 'category': category,
                    **({'requested_declaration_id': declaration_id} if declaration_id is not None else {})}
                if lease is None:
                    frontier = scheduler.build_frontier(proof)
                    formal = [item for item in frontier if item.category == 'formal-run']
                    if formal:
                        outcome = self._blocked(proof, 'waiting-on-lease', error_code='FORMAL_WORK_BUSY')
                    elif service._status(proof)['formal_verified']:
                        outcome = self._blocked(proof, 'idle')
                    elif any(node.fields.get('status') == 'replay-pending'
                            for node in service.view.list_nodes(proof, 'Certificate')):
                        outcome = self._blocked(proof, 'waiting-on-review', error_code='INDEPENDENT_REPLAY_REQUIRED')
                    else:
                        outcome = self._blocked(proof, 'blocked', error_code='FORMALIZATION_REQUIRED',
                            required_work='An aligned source-backed declaration or configured research worker is required.')
                    state.update(phase='finished', outcome=outcome)
                    self._save(operation, state)
                    return outcome
                try:
                    selected = service.view.node(lease.selected_move_id)
                    # Existing provider runs must be reattached through their
                    # original operation; their checkpoint is not frontier resume.
                    if selected.label != 'FormalDeclaration':
                        service.formal._release(lease)
                        outcome = self._blocked(proof, 'blocked', selected_move_id=selected.node_id,
                            error_code='FORMAL_RUN_RECONCILIATION_REQUIRED')
                        state.update(phase='finished', outcome=outcome)
                        self._save(operation, state)
                        return outcome
                    claims = {e.dst_id for e in service.view.edges_from(selected.node_id, 'FORMALIZES')}
                    claims.update(e.src_id for e in service.view.edges_to(selected.node_id, 'FORMALIZES'))
                    if selected.fields.get('claim_id'):
                        claims.add(selected.fields['claim_id'])
                    for edge in service.view.edges_to(selected.node_id, 'ALIGNS_DECLARATION'):
                        claims.update(e.dst_id for e in service.view.edges_from(edge.src_id, 'ALIGNS_CLAIM'))
                    claims = {key for key in claims if service.view.node(key)
                        and service.view.node(key).label == 'Claim' and key.startswith(proof + '/')}
                    if len(claims) != 1:
                        raise ValueError('selected declaration needs one committed claim association')
                    state.update(phase='selected', selected_move_id=selected.node_id,
                        declaration_id=selected.node_id, claim_id=next(iter(claims)),
                        selection_lease=lease.to_dict(), budget=budget or {'max_steps': service.formal.config.max_steps,
                            'wall_clock_ms': service.formal.config.wall_clock_ms})
                    self._save(operation, state)  # BEFORE graph preparation or provider delivery.
                except Exception:
                    service.formal._release(lease)
                    raise
        if on_progress:
            on_progress({'proof_id': proof, 'phase': 'cycle-selected', 'lifecycle': 'running',
                'cycle_disposition': 'selected', 'selected_move_id': state['selected_move_id']})
        try:
            outcome = service.formal.run_selected(operation, state, cancelled=cancelled, on_progress=on_progress)
        except Exception:
            with service.lock:
                service.formal._release(SimpleNamespace(**state['selection_lease']))
            raise
        with service.lock:
            if cancelled() and not service.jobs.formal_state(operation):
                service.formal._release(SimpleNamespace(**state['selection_lease']))
        outcome = {**outcome, 'selected_move_id': state['selected_move_id'],
            'cycle_disposition': 'cancelled' if outcome['lifecycle'] == 'cancelled' else 'formal-executed'}
        state.update(phase='finished', outcome=outcome)
        self._save(operation, state)
        return outcome
