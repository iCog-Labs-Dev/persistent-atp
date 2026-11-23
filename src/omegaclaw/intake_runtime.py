"""Opt-in, receiver-owned formalization and exact statement review.

LLM output is a draft, never verification. Each stage saves its inputs/output
before a gated mutation. Search and independent replay remain provider work.
"""
from dataclasses import dataclass
import json
import math
import os
import re

from alignment.formalizer import FormalizerWorker, LLMFormalizerBackend
from alignment.llm_client import HTTPLLMClient
from alignment.models import AlignmentCriteria, AlignmentReviewRequest, AlignmentReviewResult
from alignment.reviewer import LLMAlignmentReviewer
from alignment.worker import build_alignment_proposal
from commit_gate.canon import canonical_json, content_hash
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import LeaseCandidate
from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict, WorkerClass
from context_compiler import CompileRequest, PacketKind, TextBudget, compile_formalization
from mathproof.formal_v2 import MANIFEST_HASH


@dataclass(frozen=True)
class IntakeConfig:
    provider: str
    toolchain: str
    imports: tuple[str, ...] = ('Init',)
    text_tokens: int = 8192
    timeout: float = 60
    lemma_reuse: str = 'disabled'

    def __post_init__(self):
        if self.provider not in {'DeepSeek', 'ASIOne'}:
            raise ValueError('intake provider must be DeepSeek or ASIOne')
        if not isinstance(self.toolchain, str) or not self.toolchain.strip():
            raise ValueError('intake requires an explicitly pinned Lean toolchain')
        if (not isinstance(self.imports, tuple) or not self.imports
                or any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', x) for x in self.imports)):
            raise ValueError('intake imports must be trusted Lean module names')
        if type(self.text_tokens) is not int or not 256 <= self.text_tokens <= 32768:
            raise ValueError('intake context budget must be within 256..32768 tokens')
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 120:
            raise ValueError('intake timeout must be within (0, 120] seconds')
        if self.lemma_reuse not in {'disabled', 'source-prefix', 'simp-prefix'}:
            raise ValueError('unknown verified lemma reuse policy')
        if self.lemma_reuse != 'disabled' and self.imports != ('Init',):
            raise ValueError('initial verified lemma reuse requires Init-only imports')

    @classmethod
    def from_environment(cls):
        provider = os.environ.get('PERSISTENT_ATP_INTAKE_PROVIDER', '')
        if not provider:
            return None
        return cls(provider, os.environ.get('PERSISTENT_ATP_INTAKE_TOOLCHAIN', ''),
            tuple(os.environ.get('PERSISTENT_ATP_INTAKE_IMPORTS', 'Init').split(',')),
            int(os.environ.get('PERSISTENT_ATP_INTAKE_TEXT_TOKENS', '8192')),
            float(os.environ.get('PERSISTENT_ATP_INTAKE_TIMEOUT', '60')),
            os.environ.get('PERSISTENT_ATP_INTAKE_LEMMA_REUSE', 'disabled'))

    def llm(self):
        if self.provider == 'DeepSeek':
            key, model = os.environ.get('DEEPSEEK_API_KEY'), os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')
            base = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
        else:
            key, model = os.environ.get('ASIONE_API_KEY'), os.environ.get('ASIONE_MODEL', 'asi1-mini')
            base = os.environ.get('ASIONE_BASE_URL', 'https://llm.a.singularitycompute.com/v1')
        if not key:
            raise ValueError('configured intake provider API key is missing')
        return HTTPLLMClient(endpoint_url=base.rstrip('/') + '/chat/completions', api_key=key,
            model=model, timeout_seconds=self.timeout)

    def checkpoint_hash(self):
        values = dict(self.__dict__)
        if self.lemma_reuse == 'disabled':
            values.pop('lemma_reuse')  # Preserve pre-reuse checkpoint identities.
        return content_hash(values)


class _SavedCandidate:
    def __init__(self, candidate): self.candidate = candidate
    backend_id = 'saved-llm-draft'
    def formalize(self, compilation): return self.candidate


class IntakeWorkflow:
    def __init__(self, service, config):
        self.service, self.config = service, config

    def _save(self, operation, state):
        with self.service.lock:
            self.service.jobs.save_intake(operation, state)

    def _release(self, lease):
        self.service.store.release_lease(lease['proof_id'], lease['lease_id'], fencing_token=lease['fencing_token'])

    def _lease(self, proof, selected, worker, *, conflicts=()):
        lease = self.service.store.claim_next(proof, [LeaseCandidate(selected, {}, conflicts)],
            actor='omega-' + worker, worker_class=worker, base_revision=self.service.store.head(proof)[0],
            ttl_seconds=self.config.timeout + 120)
        if lease is None:
            raise ValueError('intake work is already leased; reconcile its owner')
        return lease.to_dict()

    def _commit(self, lease, ops, *, operation=None, state=None):
        proposal = Proposal(lease['proof_id'], lease['actor'], lease['worker_class'],
            ops=tuple(ops), base_revision=lease['base_revision'], lease_id=lease['lease_id'],
            fencing_token=lease['fencing_token'], selected_move_id=lease['selected_move_id'])
        if state is not None and self._owned_attempt(operation, state):
            self._validate_history(operation, state)
            prepared = state.setdefault('commit_proposals', [])
            if proposal.to_dict() not in prepared:
                prepared.append(proposal.to_dict())
                self._save(operation, state)  # BEFORE the journal append.
        result = self.service.gate.commit(proposal)
        if not result.accepted:
            raise ValueError('gate rejected intake: ' + ', '.join(r.reason.value for r in result.rejections))
        self.service.catch_up(lease['proof_id'])

    @staticmethod
    def _owned_hypothesis(operation, state):
        return state['claim']['fields'].get('hypothesis_operation_id') == operation

    def _owned_attempt(self, operation, state):
        from .bounded_ledger import BoundedLedger
        return (self._owned_hypothesis(operation, state) or bool(state.get('refinement_artifact'))
            or bool(BoundedLedger(self.service.jobs).child_owner(operation)))

    def _validate_history(self, operation, state):
        if state.get('refinement_artifact'):
            return self.service.refinement.validate_history(operation)
        if not self._owned_hypothesis(operation,state):
            return self.service.bounded.validate_child_history(operation)
        return self.validate_hypothesis_history(operation, state['proof_id'],
            self.service.view.node(state['claim_id']))

    def validate_hypothesis_history(self, operation, proof, claim):
        """Only this operation's admission and prepared intake writes may advance its revision."""
        fields = claim.fields
        if fields.get('hypothesis_operation_id') != operation:
            raise ValueError('hypothesis admission ownership changed')
        expected = fields.get('hypothesis_admission_revision')
        if type(expected) is not int or expected < 0:
            raise ValueError('hypothesis admission revision is invalid')
        hypothesis = fields['source_hypothesis_id']
        edge_id = f"{proof}/promote-{content_hash([hypothesis, claim.node_id])[7:]}"
        events = self.service.store.read_events_after(proof, expected)
        admission_ops = [UpsertNode('Claim', claim.node_id, dict(fields)).to_dict(),
            AddEdge('PROMOTED_TO', hypothesis, claim.node_id, edge_id).to_dict()]
        if (not events or events[0][0] != expected + 1
                or events[0][1].get('base_revision') != expected
                or events[0][1].get('worker_class') != 'coordinator'
                or events[0][1].get('selected_move_id') != hypothesis
                or events[0][1].get('ops') != admission_ops):
            raise ValueError('committed hypothesis admission differs from the original operation')
        state = self.service.jobs.intake_state(operation)
        prepared = state.get('commit_proposals', [])
        if any(event not in prepared for _, event in events[1:]):
            raise ValueError('proof changed outside the hypothesis formalization operation')
        return events[0][1]

    def _resume_lease(self, operation, state, key, *, conflicts=()):
        """Reacquire an expired intake lease only at the same validated revision."""
        saved = state[key]
        for row in self.service.store.active_leases(saved['proof_id']):
            if row.lease_id == saved['lease_id'] and row.fencing_token == saved['fencing_token']:
                if row.to_dict() != saved:
                    raise ValueError('saved intake lease identity changed')
                return
        if self.service.store.head(saved['proof_id'])[0] != saved['base_revision']:
            raise ValueError('proof changed before intake lease recovery')
        state[key] = self._lease(saved['proof_id'], saved['selected_move_id'], saved['worker_class'], conflicts=conflicts)
        self._save(operation, state)

    def _stage_lease(self, operation, state, key, worker, *, selected=None):
        if not self._owned_attempt(operation, state) and key != 'clarification_lease':
            return self._lease(state['proof_id'], selected or state['declaration_id'], worker)
        if key in state:
            self._resume_lease(operation, state, key)
        else:
            state[key] = self._lease(state['proof_id'], selected or state['declaration_id'], worker)
            self._save(operation, state)
        return state[key]

    def _clarification(self, operation, state, reason=None):
        """Persist refusal evidence without creating or weakening a declaration."""
        with self.service.lock:
            self._validate_inputs(state)
            if 'clarification' not in state:
                candidate = state.get('candidate')
                notes = candidate.get('notes') if isinstance(candidate, dict) else None
                reasons = ([notes[:4096]] if isinstance(notes, str) and notes.strip() else [])
                reasons.append(str(reason or 'The candidate is not a valid Lean proposition.')[:2048])
                report = {'format': 'omega-formalization-clarification-v1', 'operation_id': operation,
                    'proof_id': state['proof_id'], 'claim_id': state['claim_id'],
                    'spec_hash': content_hash(state['spec']), 'environment_hash': state['environment_hash'],
                    'candidate': candidate, 'clarification_reasons': reasons}
                state['clarification'] = {'report_artifact': self.service.artifacts.put(canonical_json(report)),
                    'clarification_reasons': reasons}
                self._save(operation, state)
            details = state['clarification']
            ref = details['report_artifact']
            expected_report = {'format': 'omega-formalization-clarification-v1', 'operation_id': operation,
                'proof_id': state['proof_id'], 'claim_id': state['claim_id'],
                'spec_hash': content_hash(state['spec']), 'environment_hash': state['environment_hash'],
                'candidate': state.get('candidate'), 'clarification_reasons': details['clarification_reasons']}
            if (ref != content_hash(expected_report)
                    or self.service.artifacts.get(ref) != canonical_json(expected_report)):
                raise ValueError('saved clarification report differs from its original inputs')
            edge_id = state['proof_id'] + '/clarification-' + content_hash(operation)[7:39]
            fields = {'artifact_hash': ref, 'media_type': 'application/json',
                'format': 'omega-formalization-clarification-v1'}
            node = self.service.view.node(ref)
            if node is None:
                self._release(state['draft_lease'])
                lease = self._stage_lease(operation, state, 'clarification_lease', 'coordinator',
                    selected=state['claim_id'])
                self._commit(lease, (UpsertNode('Artifact', ref, fields),
                    AddEdge('HAS_ARTIFACT', state['claim_id'], ref, edge_id)), operation=operation, state=state)
            elif (node.label != 'Artifact' or dict(node.fields) != fields
                    or not any(e.edge_id == edge_id and e.dst_id == ref
                        for e in self.service.view.edges_from(state['claim_id'], 'HAS_ARTIFACT'))):
                raise ValueError('committed clarification evidence differs from the saved refusal')
            outcome = {'ready': False, 'formalization_disposition': 'needs-clarification',
                'error_code': 'FORMALIZATION_NEEDS_CLARIFICATION', **details,
                'revision': self.service.store.head(state['proof_id'])[0]}
            state.update(phase='finished', outcome=outcome)
            self._save(operation, state)
            return outcome

    def _validate_inputs(self, state):
        if state.get('verified_lemma'):
            from .lemma_reuse import validate_binding
            validate_binding(self.service, state['verified_lemma'])
        for record in [state['claim'], *state['dependencies']]:
            current = self.service.view.node(record['id'])
            if current is None or current.label != record['label'] or dict(current.fields) != record['fields']:
                raise ValueError('formalization inputs changed; reconcile the saved draft and review')
        current = sorted([record['id'], edge.dst_id] for record in [state['claim'], *state['dependencies']]
            for edge in self.service.view.edges_from(record['id'], 'DEPENDS_ON'))
        if current != state['dependency_edges']:
            raise ValueError('formalization dependencies changed; recompile the complete context')

    def _source(self, result, *, lemma=None):
        # Only one proposition is interpolated. Imports and commands are owned
        # by the receiver; the unresolved body cannot masquerade as a proof.
        if (any(x in result.lean_type for x in ('\n', '\r', '--', '/-', '-/', ';', '"', '`'))
                or result.universe_level):
            raise ValueError('intake requires a single-line proposition with no commands or custom universe declarations')
        prelude = '\n'.join('import ' + x for x in self.config.imports)
        if lemma is not None:
            from .lemma_reuse import prefix_source
            if result.lean_name == lemma['lean_name']:
                raise ValueError('parent declaration must not shadow its verified lemma')
            prelude = prefix_source(lemma, mode=self.config.lemma_reuse)
        return (prelude
            + f'\n\ntheorem {result.lean_name} : {result.lean_type} := by\n  exact persistent_atp_pending_proof\n').encode()

    def _candidate_for_source(self, operation, state):
        """Bind missing administrative metadata before source/review creation.

        Keep the raw model response for audit and never repair a refused or
        missing proposition. The formalizer still validates every effective
        field, including supplied names and forbidden proof bodies.
        """
        candidate = dict(state['candidate'])
        name, proposition = candidate.get('lean_name'), candidate.get('lean_type')
        generated = (name is None or isinstance(name, str) and not name.strip()) and (
            isinstance(proposition, str) and bool(proposition.strip()))
        if generated:
            candidate['lean_name'] = 'persistent_atp_' + content_hash(state['declaration_id'])[7:]
        if 'effective_candidate' in state:
            if state['effective_candidate'] != candidate:
                raise ValueError('saved intake naming decision differs from the original candidate')
        else:
            state['effective_candidate'] = candidate
            state['lean_name_origin'] = 'host-generated' if generated else 'model'
            self._save(operation, state)
        return candidate

    def run(self, operation, proof, *, claim_id=None, cancelled=lambda: False, on_progress=None, refinement=False):
        service = self.service
        target_claim_id = claim_id or proof + '/c-1'
        if not isinstance(target_claim_id, str) or not target_claim_id.startswith(proof + '/'):
            raise ValueError('formalization claim is outside the requested proof')
        with service.lock:
            service.catch_up(proof)
            state = service.jobs.intake_state(operation)
            refinement_state = service.refinement.validate_history(operation) if refinement else None
        if state and (state.get('contract_manifest') != MANIFEST_HASH or state['proof_id'] != proof
                or state.get('claim_id') != target_claim_id
                or state['config_hash'] != self.config.checkpoint_hash()):
            raise ValueError('intake configuration or contract changed; reconcile the admitted operation')
        if not state:
            # Capability I/O happens before leasing, outside the shared lock.
            identity, _, deployment = service.formal.deployment(service.formal.client())
            with service.lock:
                claim = service.view.node(target_claim_id)
                if not claim or not claim.fields.get('theorem_spec_artifact'):
                    raise ValueError('automated intake requires the complete committed theorem specification')
                if claim.fields.get('hypothesis_operation_id') == operation:
                    self.validate_hypothesis_history(operation, proof, claim)
                if not refinement and (service.view.edges_to(target_claim_id, 'DRAFTS_CLAIM') or service.view.edges_to(target_claim_id, 'ALIGNS_CLAIM')
                        or service.view.edges_from(target_claim_id, 'FORMALIZES') or service.view.edges_to(target_claim_id, 'FORMALIZES')):
                    return {'ready': False, 'error_code': 'EXISTING_FORMALIZATION_REQUIRES_REVIEW'}
                spec = json.loads(service.artifacts.get(claim.fields['theorem_spec_artifact']))
                from .specification import validate_spec
                spec = validate_spec(spec)
                if spec['theorem'] != claim.fields['claim_text']:
                    raise ValueError('committed theorem specification differs from the literal claim')
                dependencies, pending, seen = [], [target_claim_id], {target_claim_id}
                while pending:
                    for edge in service.view.edges_from(pending.pop(), 'DEPENDS_ON'):
                        if edge.dst_id not in seen:
                            node = service.view.node(edge.dst_id)
                            if not node:
                                raise ValueError('formalization dependency is missing')
                            seen.add(edge.dst_id); pending.append(edge.dst_id)
                            dependencies.append({'id': node.node_id, 'label': node.label, 'fields': dict(node.fields)})
                lemma = None
                if self.config.lemma_reuse != 'disabled' and dependencies:
                    from .lemma_reuse import verified_lemma
                    if len(dependencies) != 1:
                        raise ValueError('controlled reuse supports exactly one verified dependency')
                    lemma = verified_lemma(service, proof, dependencies[0]['id'])
                    if lemma['environment_hash'] != deployment['environment_hash']:
                        raise ValueError('reused lemma and parent must share the pinned environment')
                lease = self._lease(proof, target_claim_id, 'formalizer')
                number = int(content_hash(['intake', operation])[7:], 16)
                state = {'contract_manifest': MANIFEST_HASH, 'config_hash': self.config.checkpoint_hash(),
                    'proof_id': proof, 'claim_id': target_claim_id, 'declaration_id': f'{proof}/fd-{number}',
                    'environment_id': f'{proof}/env-{number}', 'alignment_id': f'{proof}/al-{number}',
                    'provider_url': service.formal.config.url, 'deployment_id': identity,
                    'environment_hash': deployment['environment_hash'], 'provider_environment_id': deployment['environment_id'],
                    'claim': {'id': target_claim_id, 'label': 'Claim', 'fields': dict(claim.fields)},
                    'spec': spec, 'dependencies': dependencies, 'draft_lease': lease, 'phase': 'prepared'}
                state['dependency_edges'] = sorted([record['id'], edge.dst_id]
                    for record in [state['claim'], *dependencies]
                    for edge in service.view.edges_from(record['id'], 'DEPENDS_ON'))
                if lemma is not None:
                    state['verified_lemma'] = lemma
                if refinement_state:
                    if lemma != refinement_state['lemma'] or target_claim_id != refinement_state['claim_id']:
                        raise ValueError('refinement intake differs from its admitted verified lemma')
                    state['refinement_artifact'] = refinement_state['lineage_artifact']
                self._save(operation, state)
        try:
            if state['provider_url'] != service.formal.config.url:
                raise ValueError('intake provider changed; reconcile the admitted operation')
            if cancelled():
                return {'ready': False, 'cancelled': True}
            if state.get('phase') == 'finished':
                return state['outcome']
            if 'clarification' in state:
                return self._clarification(operation, state)
            with service.lock:
                if self._owned_attempt(operation, state):
                    self._validate_history(operation, state)
                    self._validate_inputs(state)
                    if service.view.node(state['declaration_id']) is None:
                        self._resume_lease(operation, state, 'draft_lease')
            compilation = compile_formalization(CompileRequest(operation, proof, state['draft_lease']['base_revision'],
                PacketKind.FORMALIZATION, WorkerClass.FORMALIZER, text_budget=TextBudget(self.config.text_tokens)),
                claim=state['claim'], dependencies=state['dependencies'], theorem=state['spec'],
                verified_premises=([{key: state['verified_lemma'][key] for key in
                    ('claim_id', 'lean_name', 'lean_type', 'certificate_id')}]
                    if state.get('verified_lemma') else ()))
            if 'candidate' not in state:
                from .bounded_ledger import BoundedLedger,BoundedBlocked
                if BoundedLedger(service.jobs).child_owner(operation):
                    if state.get('bounded_draft_started'):
                        raise BoundedBlocked('formalization response delivery is uncertain; retain the original bounded step')
                    state['bounded_draft_started']=True;self._save(operation,state)
                try:
                    candidate = LLMFormalizerBackend(self.config.llm()).formalize(compilation)
                except ValueError as exc:
                    return self._clarification(operation, state, exc)
                state['candidate'] = dict(candidate)
                self._save(operation, state)
            try:
                candidate = self._candidate_for_source(operation, state)
                result = FormalizerWorker(_SavedCandidate(candidate), actor=state['draft_lease']['actor']).run(
                    compilation, declaration_id=state['declaration_id'], claim_id=state['claim_id'],
                    base_revision=state['draft_lease']['base_revision'], lease_id=state['draft_lease']['lease_id'],
                    fencing_token=state['draft_lease']['fencing_token'])
                source_bytes = self._source(result, lemma=state.get('verified_lemma'))
            except ValueError as exc:
                return self._clarification(operation, state, exc)
            if cancelled():
                return {'ready': False, 'cancelled': True}
            with service.lock:
                self._validate_inputs(state)
                if service.view.node(state['declaration_id']) is None:
                    self._commit(state['draft_lease'], result.proposal.ops, operation=operation, state=state)
                self._release(state['draft_lease'])
                declaration = service.view.node(state['declaration_id'])
                if any(declaration.fields.get(k) != v for k, v in (('lean_name', result.lean_name), ('lean_type', result.lean_type))):
                    raise ValueError('committed draft differs from saved formalizer output')
                source = service.artifacts.put(source_bytes)
                if not declaration.fields.get('lean_source_artifact'):
                    lease = self._stage_lease(operation, state, 'source_lease', 'coordinator')
                    try:
                        ops = [SetField('FormalDeclaration', state['declaration_id'], key, value, prior=None)
                            for key, value in {'lean_source_artifact': source, 'source_hash': source,
                                'claim_id': state['claim_id'], 'intake_operation_id': operation}.items()]
                        if state.get('verified_lemma'):
                            binding = service.artifacts.put(canonical_json(state['verified_lemma']))
                            ops.append(SetField('FormalDeclaration', state['declaration_id'],
                                'lemma_reuse_artifact', binding, prior=None))
                        if state.get('refinement_artifact'):
                            ops.append(SetField('FormalDeclaration', state['declaration_id'],
                                'parent_refinement_artifact', state['refinement_artifact'], prior=None))
                        ops += [UpsertNode('Environment', state['environment_id'], {'toolchain': self.config.toolchain,
                            'environment_hash': state['environment_hash'], 'provider_environment_id': state['provider_environment_id']}),
                            AddEdge('PINNED_ENVIRONMENT', state['declaration_id'], state['environment_id'], state['environment_id'] + '-pin')]
                        self._commit(lease, ops, operation=operation, state=state)
                    finally:
                        self._release(lease)
                declaration = service.view.node(state['declaration_id'])
                if declaration.fields.get('lean_source_artifact') != source or declaration.fields.get('intake_operation_id') != operation:
                    raise ValueError('source-backed declaration ownership changed')
                if 'source_lease' in state:
                    self._release(state['source_lease'])
                state['source_hash'] = source
                state['packet_artifact'] = service.artifacts.put(compilation.context_text.encode())
                if 'review_lease' not in state:
                    state['review_lease'] = self._lease(proof, state['declaration_id'], 'alignment-reviewer', conflicts=(state['claim_id'],))
                elif self._owned_attempt(operation, state) and service.view.node(state['alignment_id']) is None:
                    self._resume_lease(operation, state, 'review_lease', conflicts=(state['claim_id'],))
                self._save(operation, state)
            if on_progress:
                on_progress({'proof_id': proof, 'phase': 'alignment-review', 'lifecycle': 'running'})
            request = AlignmentReviewRequest(proof, state['claim_id'], state['declaration_id'],
                state['claim']['fields']['claim_text'] + '\n\nCOMPLETE COMMITTED SPECIFICATION:\n' + json.dumps(state['spec'], ensure_ascii=False),
                source_bytes.decode() + '\nThe unresolved body is only a search placeholder. Review the proposition, not proof completion.',
                imports=self.config.imports, statement_hash=state['claim']['fields']['literal_target_hash'], source_hash=source)
            if 'review' not in state:
                from .bounded_ledger import BoundedLedger,BoundedBlocked
                if BoundedLedger(service.jobs).child_owner(operation):
                    if state.get('bounded_review_started'):
                        raise BoundedBlocked('alignment response delivery is uncertain; retain the original bounded step')
                    state['bounded_review_started']=True;self._save(operation,state)
                review = LLMAlignmentReviewer(self.config.llm(), reviewer_id=state['review_lease']['actor'], strict=True).review(request)
                state['review'] = {'verdict': review.verdict.value, 'lifecycle': review.lifecycle.value,
                    'criteria': review.criteria.to_dict(), 'reviewer': review.reviewer, 'reasoning': review.reasoning}
                self._save(operation, state)
            raw = state['review']
            criteria = AlignmentCriteria(**raw['criteria'])
            accepted = (raw['verdict'] == 'aligned' and raw['lifecycle'] == 'reviewed' and criteria.relation == 'exact'
                and criteria.selected_definitions_match is True and criteria.implication_to_target_explicit is True)
            review = AlignmentReviewResult(AlignmentVerdict(raw['verdict']) if accepted else AlignmentVerdict.AMBIGUOUS,
                AlignmentLifecycle.REVIEWED if accepted else AlignmentLifecycle.REVIEW_NEEDED, criteria,
                state['review_lease']['actor'], raw['reasoning'])
            if cancelled():
                return {'ready': False, 'cancelled': True}
            with service.lock:
                self._validate_inputs(state)
                lease = state['review_lease']
                report = service.artifacts.put(canonical_json({'review': raw, 'spec_hash': content_hash(state['spec']),
                    'source_hash': source, 'environment_hash': state['environment_hash'], 'packet_artifact': state['packet_artifact']}))
                alignment = service.view.node(state['alignment_id'])
                if alignment is None:
                    proposal = build_alignment_proposal(proof_id=proof, alignment_id=state['alignment_id'], request=request,
                        result=review, actor=lease['actor'], base_revision=lease['base_revision'], lease_id=lease['lease_id'],
                        fencing_token=lease['fencing_token'], selected_move_id=lease['selected_move_id'])
                    first = proposal.ops[0]
                    self._commit(lease, (UpsertNode(first.label, first.node_id, {**first.fields, 'report_artifact': report}), *proposal.ops[1:]),
                        operation=operation, state=state)
                elif alignment.fields.get('report_artifact') != report:
                    raise ValueError('committed alignment differs from saved review')
                self._release(lease)
                declaration = service.view.node(state['declaration_id'])
                if accepted and declaration.fields['status'] == 'draft':
                    authority = self._stage_lease(operation, state, 'promotion_lease', 'coordinator')
                    try:
                        self._commit(authority, [SetField('FormalDeclaration', state['declaration_id'], 'status', 'aligned', prior='draft')],
                            operation=operation, state=state)
                    finally:
                        self._release(authority)
                outcome = {'ready': accepted, 'declaration_id': state['declaration_id'], 'alignment_id': state['alignment_id'],
                    'report_artifact': report, 'revision': service.store.head(proof)[0],
                    **({} if accepted else {'error_code': 'EXACT_ALIGNMENT_REVIEW_REQUIRED'})}
                state.update(phase='finished', outcome=outcome)
                self._save(operation, state)
                return outcome
        finally:
            # Preserve leases on process interruption. Normal completion/error
            # never releases another operation's lease or silently renews it.
            import sys
            if sys.exc_info()[0] is None or issubclass(sys.exc_info()[0], Exception):
                with service.lock:
                    for key in ('draft_lease', 'review_lease', 'source_lease', 'promotion_lease', 'clarification_lease'):
                        if key in state:
                            self._release(state[key])
