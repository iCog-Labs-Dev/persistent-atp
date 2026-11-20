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

    @classmethod
    def from_environment(cls):
        provider = os.environ.get('PERSISTENT_ATP_INTAKE_PROVIDER', '')
        if not provider:
            return None
        return cls(provider, os.environ.get('PERSISTENT_ATP_INTAKE_TOOLCHAIN', ''),
            tuple(os.environ.get('PERSISTENT_ATP_INTAKE_IMPORTS', 'Init').split(',')),
            int(os.environ.get('PERSISTENT_ATP_INTAKE_TEXT_TOKENS', '8192')),
            float(os.environ.get('PERSISTENT_ATP_INTAKE_TIMEOUT', '60')))

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

    def _commit(self, lease, ops):
        result = self.service.gate.commit(Proposal(lease['proof_id'], lease['actor'], lease['worker_class'],
            ops=tuple(ops), base_revision=lease['base_revision'], lease_id=lease['lease_id'],
            fencing_token=lease['fencing_token'], selected_move_id=lease['selected_move_id']))
        if not result.accepted:
            raise ValueError('gate rejected intake: ' + ', '.join(r.reason.value for r in result.rejections))
        self.service.catch_up(lease['proof_id'])

    def _validate_inputs(self, state):
        for record in [state['claim'], *state['dependencies']]:
            current = self.service.view.node(record['id'])
            if current is None or current.label != record['label'] or dict(current.fields) != record['fields']:
                raise ValueError('formalization inputs changed; reconcile the saved draft and review')
        current = sorted([record['id'], edge.dst_id] for record in [state['claim'], *state['dependencies']]
            for edge in self.service.view.edges_from(record['id'], 'DEPENDS_ON'))
        if current != state['dependency_edges']:
            raise ValueError('formalization dependencies changed; recompile the complete context')

    def _source(self, result):
        # Only one proposition is interpolated. Imports and commands are owned
        # by the receiver; the unresolved body cannot masquerade as a proof.
        if (any(x in result.lean_type for x in ('\n', '\r', '--', '/-', '-/', ';', '"', '`'))
                or result.universe_level):
            raise ValueError('intake requires a single-line proposition with no commands or custom universe declarations')
        return ('\n'.join('import ' + x for x in self.config.imports)
            + f'\n\ntheorem {result.lean_name} : {result.lean_type} := by\n  exact persistent_atp_pending_proof\n').encode()

    def run(self, operation, proof, *, claim_id=None, cancelled=lambda: False, on_progress=None):
        service = self.service
        target_claim_id = claim_id or proof + '/c-1'
        if not isinstance(target_claim_id, str) or not target_claim_id.startswith(proof + '/'):
            raise ValueError('formalization claim is outside the requested proof')
        with service.lock:
            state = service.jobs.intake_state(operation)
        if state and (state.get('contract_manifest') != MANIFEST_HASH or state['proof_id'] != proof
                or state.get('claim_id') != target_claim_id
                or state['config_hash'] != content_hash(self.config.__dict__)):
            raise ValueError('intake configuration or contract changed; reconcile the admitted operation')
        if not state:
            # Capability I/O happens before leasing, outside the shared lock.
            identity, _, deployment = service.formal.deployment(service.formal.client())
            with service.lock:
                claim = service.view.node(target_claim_id)
                if not claim or not claim.fields.get('theorem_spec_artifact'):
                    raise ValueError('automated intake requires the complete committed theorem specification')
                if (service.view.edges_to(target_claim_id, 'DRAFTS_CLAIM') or service.view.edges_to(target_claim_id, 'ALIGNS_CLAIM')
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
                lease = self._lease(proof, target_claim_id, 'formalizer')
                number = int(content_hash(['intake', operation])[7:], 16)
                state = {'contract_manifest': MANIFEST_HASH, 'config_hash': content_hash(self.config.__dict__),
                    'proof_id': proof, 'claim_id': target_claim_id, 'declaration_id': f'{proof}/fd-{number}',
                    'environment_id': f'{proof}/env-{number}', 'alignment_id': f'{proof}/al-{number}',
                    'provider_url': service.formal.config.url, 'deployment_id': identity,
                    'environment_hash': deployment['environment_hash'], 'provider_environment_id': deployment['environment_id'],
                    'claim': {'id': target_claim_id, 'label': 'Claim', 'fields': dict(claim.fields)},
                    'spec': spec, 'dependencies': dependencies, 'draft_lease': lease, 'phase': 'prepared'}
                state['dependency_edges'] = sorted([record['id'], edge.dst_id]
                    for record in [state['claim'], *dependencies]
                    for edge in service.view.edges_from(record['id'], 'DEPENDS_ON'))
                self._save(operation, state)
        try:
            if state['provider_url'] != service.formal.config.url:
                raise ValueError('intake provider changed; reconcile the admitted operation')
            if cancelled():
                return {'ready': False, 'cancelled': True}
            if state.get('phase') == 'finished':
                return state['outcome']
            compilation = compile_formalization(CompileRequest(operation, proof, state['draft_lease']['base_revision'],
                PacketKind.FORMALIZATION, WorkerClass.FORMALIZER, text_budget=TextBudget(self.config.text_tokens)),
                claim=state['claim'], dependencies=state['dependencies'], theorem=state['spec'])
            if 'candidate' not in state:
                candidate = LLMFormalizerBackend(self.config.llm()).formalize(compilation)
                state['candidate'] = dict(candidate)
                self._save(operation, state)
            result = FormalizerWorker(_SavedCandidate(state['candidate']), actor=state['draft_lease']['actor']).run(
                compilation, declaration_id=state['declaration_id'], claim_id=state['claim_id'],
                base_revision=state['draft_lease']['base_revision'], lease_id=state['draft_lease']['lease_id'],
                fencing_token=state['draft_lease']['fencing_token'])
            source_bytes = self._source(result)
            if cancelled():
                return {'ready': False, 'cancelled': True}
            with service.lock:
                self._validate_inputs(state)
                if service.view.node(state['declaration_id']) is None:
                    self._commit(state['draft_lease'], result.proposal.ops)
                self._release(state['draft_lease'])
                declaration = service.view.node(state['declaration_id'])
                if any(declaration.fields.get(k) != v for k, v in (('lean_name', result.lean_name), ('lean_type', result.lean_type))):
                    raise ValueError('committed draft differs from saved formalizer output')
                source = service.artifacts.put(source_bytes)
                if not declaration.fields.get('lean_source_artifact'):
                    lease = self._lease(proof, state['declaration_id'], 'coordinator')
                    try:
                        ops = [SetField('FormalDeclaration', state['declaration_id'], key, value, prior=None)
                            for key, value in {'lean_source_artifact': source, 'source_hash': source,
                                'claim_id': state['claim_id'], 'intake_operation_id': operation}.items()]
                        ops += [UpsertNode('Environment', state['environment_id'], {'toolchain': self.config.toolchain,
                            'environment_hash': state['environment_hash'], 'provider_environment_id': state['provider_environment_id']}),
                            AddEdge('PINNED_ENVIRONMENT', state['declaration_id'], state['environment_id'], state['environment_id'] + '-pin')]
                        self._commit(lease, ops)
                    finally:
                        self._release(lease)
                declaration = service.view.node(state['declaration_id'])
                if declaration.fields.get('lean_source_artifact') != source or declaration.fields.get('intake_operation_id') != operation:
                    raise ValueError('source-backed declaration ownership changed')
                state['source_hash'] = source
                state['packet_artifact'] = service.artifacts.put(compilation.context_text.encode())
                if 'review_lease' not in state:
                    state['review_lease'] = self._lease(proof, state['declaration_id'], 'alignment-reviewer', conflicts=(state['claim_id'],))
                self._save(operation, state)
            if on_progress:
                on_progress({'proof_id': proof, 'phase': 'alignment-review', 'lifecycle': 'running'})
            request = AlignmentReviewRequest(proof, state['claim_id'], state['declaration_id'],
                state['claim']['fields']['claim_text'] + '\n\nCOMPLETE COMMITTED SPECIFICATION:\n' + json.dumps(state['spec'], ensure_ascii=False),
                source_bytes.decode() + '\nThe unresolved body is only a search placeholder. Review the proposition, not proof completion.',
                imports=self.config.imports, statement_hash=state['claim']['fields']['literal_target_hash'], source_hash=source)
            if 'review' not in state:
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
                    self._commit(lease, (UpsertNode(first.label, first.node_id, {**first.fields, 'report_artifact': report}), *proposal.ops[1:]))
                elif alignment.fields.get('report_artifact') != report:
                    raise ValueError('committed alignment differs from saved review')
                self._release(lease)
                declaration = service.view.node(state['declaration_id'])
                if accepted and declaration.fields['status'] == 'draft':
                    authority = self._lease(proof, state['declaration_id'], 'coordinator')
                    try:
                        self._commit(authority, [SetField('FormalDeclaration', state['declaration_id'], 'status', 'aligned', prior='draft')])
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
                    for key in ('draft_lease', 'review_lease'):
                        if key in state:
                            self._release(state[key])
