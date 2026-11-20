"""Trusted receiver driver over PR85's formal-v2 client and admission helpers.

Provider requests and leases remain in the existing operational ledger, outside
Omega's cache. Network work never holds the journal connection's service lock.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import time
import os
from urllib.parse import urlsplit
from types import SimpleNamespace

from commit_gate.canon import canonical_json, content_hash
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from context_compiler.contracts import FormalExecutionLimits
from context_compiler.formal import CheckpointPolicy, PremiseAccessibility
from context_compiler.formal_runtime import FormalLeaseCompiler, FormalRuntimeConfig
from mathproof.formal_v2 import MANIFEST_HASH
from mathproof.maths_ai_http import MathsAIAPIError as FormalV2Error, MathsAIHTTPFormalATP
from mathproof.formal_v2_admission import admit_search, admit_replay, admit_promotion
from mathproof.scheduler import GlobalScheduler


class ReplayReconciliationRequired(FormalV2Error):
    def __init__(self, message):
        super().__init__(message, code='FORMAL_REPLAY_RECONCILIATION_REQUIRED')
    public_summary = 'Independent replay delivery is uncertain; reconcile its receipt before authorizing another replay.'


class ContractMigrationRequired(FormalV2Error):
    public_summary = 'This operation uses an earlier formal contract; reconcile it before starting a new operation.'

    def __init__(self):
        super().__init__(self.public_summary, code='FORMAL_CONTRACT_MIGRATION_REQUIRED')


class _Artifacts:
    """Serialize local repository access; provider I/O never holds this lock."""
    def __init__(self, service):
        self.service = service

    def _call(self, method, *args):
        if self.service is None:
            raise FormalV2Error('readiness checks cannot access execution artifacts')
        with self.service.lock:
            return getattr(self.service.artifacts, method)(*args)

    def get(self, key): return self._call('get', key)
    def put(self, raw): return self._call('put', raw)
    def exists(self, key): return self._call('exists', key)


@dataclass(frozen=True)
class FormalProviderConfig:
    url: str
    deployment_id: str | None = None
    profile: str | None = None
    token: str | None = None
    timeout: float = 90
    max_steps: int = 100
    wall_clock_ms: int = 30000
    replay_steps: int = 128
    replay_wall_clock_ms: int = 60000
    poll_seconds: float = .25
    mode: str = 'real'

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('formal endpoint cannot embed credentials, queries, or fragments')
        for name in ('max_steps', 'wall_clock_ms', 'replay_steps', 'replay_wall_clock_ms'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.mode not in {'real', 'scripted-test'} or not .05 <= self.poll_seconds <= 5:
            raise ValueError('invalid formal execution mode or poll interval')
        if self.replay_wall_clock_ms / 1000 > self.timeout:
            raise ValueError('formal HTTP timeout must cover independent replay allowance')
        if not 0 < self.timeout <= 120:
            raise ValueError('formal HTTP timeout must be within (0, 120] seconds')
        MathsAIHTTPFormalATP(self.url, artifacts=_Artifacts(None), timeout=self.timeout)

    @classmethod
    def from_environment(cls):
        url = os.environ.get('PERSISTENT_ATP_FORMAL_URL', '')
        if not url:
            raise ValueError('PERSISTENT_ATP_FORMAL_URL is required')
        return cls(url=url, deployment_id=os.environ.get('PERSISTENT_ATP_FORMAL_DEPLOYMENT') or None,
            profile=os.environ.get('PERSISTENT_ATP_FORMAL_PROFILE') or None,
            token=os.environ.get('PERSISTENT_ATP_FORMAL_TOKEN') or None,
            max_steps=int(os.environ.get('PERSISTENT_ATP_FORMAL_MAX_STEPS', '100')),
            wall_clock_ms=int(os.environ.get('PERSISTENT_ATP_FORMAL_WALL_CLOCK_MS', '30000')),
            replay_steps=int(os.environ.get('PERSISTENT_ATP_FORMAL_REPLAY_STEPS', '128')),
            replay_wall_clock_ms=int(os.environ.get('PERSISTENT_ATP_FORMAL_REPLAY_WALL_CLOCK_MS', '60000')),
            timeout=float(os.environ.get('PERSISTENT_ATP_FORMAL_HTTP_TIMEOUT', '90')))


class _Evidence:
    """Already validated network evidence, consumed offline under the gate lock."""
    def __init__(self, binding, canonical, artifacts, receipt=None):
        self.value, self.canonical, self.artifacts, self.receipt = deepcopy(binding), deepcopy(canonical), artifacts, receipt
        run = binding['request']['run_id']
        self.requests = {run: binding['request']}
        self.latest = {run: binding['snapshot']}

    def run_evidence(self, run_id):
        return deepcopy(self.latest[run_id]), deepcopy(self.canonical)

    def request_for(self, run_id):
        return deepcopy(self.requests[run_id])

    def binding(self, run_id):
        assert run_id in self.requests
        return deepcopy(self.value)

    def get_artifact(self, address):
        return self.artifacts[address]

    def restore(self, binding):
        if binding != self.value:
            raise FormalV2Error('persisted search binding changed')

    def replay_attestation(self, *args, **kwargs):
        if self.receipt is None:
            raise FormalV2Error('independent replay receipt is unavailable')
        return deepcopy(self.receipt)


class FormalWorkflow:
    def __init__(self, service, config):
        self.service, self.config = service, config
        self._callbacks = {}

    def client(self):
        return MathsAIHTTPFormalATP(self.config.url, artifacts=_Artifacts(self.service),
            timeout=self.config.timeout, token=self.config.token)

    def deployment(self, client):
        caps = client.formal_capabilities()  # Exact pinned nine-schema compatibility.
        identity = self.config.deployment_id or (caps.get('default') or {}).get('deployment_id')
        deployment = caps['deployments'].get(identity)
        if not deployment or not deployment['available'] or not deployment.get('environment_hash'):
            raise FormalV2Error('configured formal deployment is unavailable')
        profile = self.config.profile
        if profile is None and identity == (caps.get('default') or {}).get('deployment_id'):
            profile = (caps.get('default') or {}).get('profile')
        if profile is None and len(deployment['profiles']) == 1:
            profile = next(iter(deployment['profiles']))
        if profile not in deployment['profiles']:
            raise FormalV2Error('explicit formal profile is required')
        if 'source' not in deployment['inputs']:
            raise FormalV2Error('configured deployment cannot execute committed Lean source inputs')
        return identity, profile, deployment

    def ready(self):
        try:
            client = self.client()
            client.timeout = min(client.timeout, 5)
            self.deployment(client)
            return True
        except (FormalV2Error, OSError, ValueError, KeyError, TypeError):
            return False

    def _load(self, operation):
        with self.service.lock:
            return self.service.jobs.formal_state(operation)

    def _save(self, operation, state):
        with self.service.lock:
            self.service.jobs.save_formal(operation, state)
            callback = self._callbacks.get(operation)
        if callback:
            callback(self._report(state, lifecycle='running'))

    def _release(self, lease):
        self.service.store.release_lease(lease.proof_id, lease.lease_id, fencing_token=lease.fencing_token)

    @staticmethod
    def _accepted(result):
        if not result.accepted:
            raise FormalV2Error('commit gate rejected formal evidence; review current revision and authority')

    def _prepare(self, operation, proof, claim, budget, client, deployment, *, selection=None):
        identity, profile, selected = deployment
        service = self.service
        declarations = [edge.dst_id for edge in service.view.edges_from(claim, 'FORMALIZES')]
        declarations += [edge.src_id for edge in service.view.edges_to(claim, 'FORMALIZES')]
        # Existing formalization/review paths may associate through Alignment.
        for alignment in service.view.edges_to(claim, 'ALIGNS_CLAIM'):
            declarations.extend(e.dst_id for e in service.view.edges_from(alignment.src_id, 'ALIGNS_DECLARATION'))
        declarations = sorted({name for name in declarations if service.view.node(name)
            and service.view.node(name).label == 'FormalDeclaration'
            and service.view.node(name).fields.get('status') in {'aligned', 'searching'}})
        if selection is not None:
            declarations = [name for name in declarations if name == selection['declaration_id']]
        if len(declarations) != 1:
            raise FormalV2Error('one reviewed formal declaration is required for this claim')
        declaration = service.view.node(declarations[0])
        if declaration.fields.get('claim_id') not in (None, claim):
            raise FormalV2Error('formal declaration belongs to another claim')
        if not declaration.fields.get('lean_name') or not declaration.fields.get('lean_type'):
            raise FormalV2Error('trusted formalization must supply the original Lean name and type')
        source = declaration.fields['lean_source_artifact']
        source_bytes = service.artifacts.get(source)
        if 'sha256:' + hashlib.sha256(source_bytes).hexdigest() != source:
            raise FormalV2Error('formal source bytes differ from their content address')
        pins = service.view.edges_from(declaration.node_id, 'PINNED_ENVIRONMENT')
        if len(pins) != 1:
            raise FormalV2Error('formalization must pin one environment before dispatch')
        environment = service.view.node(pins[0].dst_id)
        if (environment is None or environment.fields.get('environment_hash') != selected['environment_hash']
                or environment.fields.get('provider_environment_id') != selected['environment_id']):
            raise FormalV2Error('committed environment differs from configured provider deployment')
        number = int(content_hash(operation)[7:], 16)
        run, root, checkpoint = f'{proof}/fr-{number}', f'{proof}/fs-{number}', f'{proof}/fc-{number}'
        actor = 'omega-formal-atp'
        if selection is not None:
            original = SimpleNamespace(**selection['selection_lease'])
            if (original.proof_id != proof or original.worker_class != 'formal-atp'
                    or original.actor != actor or original.selected_move_id != declaration.node_id):
                raise FormalV2Error('cycle selection does not authorize this declaration')
        if service.view.node(run) is None:
            seed = original if selection is not None else service.store.issue_lease(
                proof, f'{proof}/prepare-{operation}', actor='omega-coordinator',
                worker_class='coordinator', selected_move_id=claim)
            try:
                # This display root is a run occurrence, never a native state identity.
                fields = {'status': 'open', 'goal_text': declaration.fields['lean_type'],
                    'environment_hash': selected['environment_hash'], 'exact_state_hash': content_hash([run, source]),
                    'semantic_signature': content_hash(declaration.fields['lean_type']),
                    'exact_hash_scope': 'run-occurrence', 'transposition_eligible': False}
                ops = (UpsertNode('FormalRun', run, {'status': 'searching', 'actor': actor,
                        'environment_hash': selected['environment_hash'], 'operation_id': operation}), UpsertNode('FormalState', root, fields),
                    UpsertNode('FormalCheckpoint', checkpoint, {}),
                    AddEdge('SEARCHES', run, declaration.node_id, f'{proof}/search-{operation}'),
                    AddEdge('RAN_UNDER', run, environment.node_id, f'{proof}/env-{operation}'),
                    AddEdge('HAS_ROOT', run, root, f'{proof}/root-{operation}'),
                    AddEdge('HAS_CHECKPOINT', run, checkpoint, f'{proof}/checkpoint-{operation}'),
                    AddEdge('CHECKPOINT_FRONTIER', checkpoint, root, f'{proof}/frontier-{operation}'))
                self._accepted(service.gate.commit(Proposal(proof, seed.actor, seed.worker_class, ops=ops,
                    base_revision=seed.base_revision, lease_id=seed.lease_id, fencing_token=seed.fencing_token,
                    selected_move_id=seed.selected_move_id)))
                service.catch_up(proof)
            finally:
                self._release(seed)
        else:
            # No prepared request means no provider mutation was sent. A crash
            # between leasing/compiling and saving can leave only our own
            # orphaned lease; release it before recompiling the same run.
            existing = service.view.node(run)
            if (existing.fields.get('actor') != actor or existing.fields.get('provider_binding_artifact')
                    or selection is not None and existing.fields.get('operation_id') != operation):
                raise FormalV2Error('formal run already belongs to another execution')
            for orphan in service.store.active_leases(proof):
                if orphan.selected_move_id == run and orphan.actor == actor and orphan.worker_class == 'formal-atp':
                    self._release(orphan)
            if selection is not None:
                self._release(original)
        lease = GlobalScheduler(service.view, service.store).lease_selected(proof, run, 'formal-atp', actor=actor,
            ttl_seconds=self.config.wall_clock_ms / 1000 + 300)
        if lease is None:
            raise FormalV2Error('formal run is not free and schedulable')
        try:
            ceiling = selected['profiles'][profile]
            config = FormalRuntimeConfig(FormalExecutionLimits(min(budget['max_steps'], self.config.max_steps, ceiling['max_steps']),
                min(budget['wall_clock_ms'], self.config.wall_clock_ms, ceiling['wall_clock_ms'])), PremiseAccessibility(frozenset()),
                CheckpointPolicy(10), 'formal-v2', protocol_version=2, kernel_id=claim,
                backend={'name': selected['backend'], 'profile': profile}, deployment_id=identity)
            dispatch = FormalLeaseCompiler(service.view, service.store, service.artifacts, config).compile(lease)
            request = client.prepare(dispatch.worker_input)
            packet = service.artifacts.put(dispatch.packet.content.encode('utf-8'))
        except BaseException:
            self._release(lease)
            raise
        return {'contract_manifest': MANIFEST_HASH, 'phase': 'prepared', 'run_id': run, 'proof_id': proof, 'claim_id': claim,
            'provider_url': self.config.url, 'request': request, 'search_lease': lease.to_dict(),
            **({'selected_move_id': selection['selected_move_id'], 'operation_kind': 'proof-cycle'} if selection else {}),
            'replay_steps': min(self.config.replay_steps, selected['verification']['max_steps']),
            'replay_wall_clock_ms': min(self.config.replay_wall_clock_ms, selected['verification']['wall_clock_ms']),
            'packet_artifact': packet, 'deadline': time.time() + self.config.wall_clock_ms / 1000 + 120}

    def run_selected(self, operation, selection, *, cancelled=lambda: False, on_progress=None):
        """Execute a durable scheduler selection through the same provider workflow."""
        return self.run(operation, selection['proof_id'], selection['claim_id'], selection['budget'],
            cancelled=cancelled, on_progress=on_progress, selection=selection)

    def run(self, operation, proof, claim, budget, cancelled=lambda: False, expected_revision=None, on_progress=None,
            *, selection=None):
        with self.service.lock:
            self._callbacks[operation] = on_progress
        try:
            return self._run(operation, proof, claim, budget, cancelled, expected_revision, selection=selection)
        finally:
            with self.service.lock:
                self._callbacks.pop(operation, None)

    def _run(self, operation, proof, claim, budget, cancelled, expected_revision, *, selection=None):
        service, client = self.service, self.client()
        state = self._load(operation)
        if state and state.get('contract_manifest') != MANIFEST_HASH:
            raise ContractMigrationRequired()
        if state and state['provider_url'] != self.config.url:
            raise FormalV2Error('configured provider changed during an admitted operation')
        if cancelled() and not state:
            return {'proof_id': proof, 'lifecycle': 'cancelled', 'component_mode': 'formal-v2-' + self.config.mode}
        if not state:
            deployment = self.deployment(client)
            with service.lock:
                if expected_revision is not None and service.store.head(proof)[0] != expected_revision:
                    raise FormalV2Error('proof changed before formal dispatch; reconsider the original intent')
                state = self._prepare(operation, proof, claim, budget, client, deployment,
                    **({'selection': selection} if selection is not None else {}))
                self._save(operation, state)  # BEFORE any provider mutation.
        run = state['run_id']
        client.restore_prepared(state['request'])
        if state.get('binding'):
            client.restore(state['binding'], refresh=False)
        while True:
            with service.lock:
                imported = service.view.node(run)
                admitted = bool(imported and imported.fields.get('provider_binding_artifact'))
            if admitted:
                break
            try:
                client.formal_search_status(run)
                snapshot, canonical = client.cached_evidence(run)
            except FormalV2Error as exc:
                if exc.status_code is None or exc.status_code >= 500:
                    if time.time() >= state['deadline']:
                        raise
                    time.sleep(self.config.poll_seconds)
                    continue
                if exc.status_code != 404:
                    raise
                if cancelled():
                    with service.lock:
                        self._release(SimpleNamespace(**state['search_lease']))
                    return self._report(state, lifecycle='cancelled')
                try:
                    # The consolidated adapter uploads the exact durable source
                    # before sending this original idempotent request.
                    client.formal_search_start(state['request'])  # Same idempotent start only.
                    snapshot, canonical = client.cached_evidence(run)
                except FormalV2Error as start_error:
                    if (start_error.status_code is None or start_error.status_code >= 500) and time.time() < state['deadline']:
                        time.sleep(self.config.poll_seconds)
                        continue  # Reconcile by GET before any further POST.
                    raise
            state.update(binding=client.binding(run), phase='searching')
            self._save(operation, state)
            if cancelled() and not snapshot.get('cancel_requested') and snapshot.get('result') is None:
                try:
                    client.formal_search_cancel(run)
                    snapshot, canonical = client.cached_evidence(run)
                except FormalV2Error as error:
                    if (error.status_code is None or error.status_code >= 500) and time.time() < state['deadline']:
                        time.sleep(self.config.poll_seconds)
                        continue  # GET reconciles whether the backend received cancellation.
                    raise
                state.update(binding=client.binding(run), cancel_requested=True)
                self._save(operation, state)
            if snapshot.get('result') is not None:
                result = snapshot['result']
                addresses = {item['sha256'] for item in result['artifacts']}
                addresses.update(value for key, value in result.items() if key in {'input_artifact_hash', 'source_artifact_hash'} and value)
                evidence = _Evidence(client.binding(run), canonical, {key: _Artifacts(service).get(key) for key in addresses})
                with service.lock:
                    self._accepted(admit_search(evidence, run, SimpleNamespace(**state['search_lease']), service.gate, service.artifacts))
                    service.catch_up(proof)
                    self._release(SimpleNamespace(**state['search_lease']))
                    state['phase'] = 'search-admitted'
                    self._save(operation, state)
                break
            if time.time() > state['deadline']:
                raise FormalV2Error('formal observation deadline exceeded; retain original request for reconciliation')
            time.sleep(self.config.poll_seconds)
        result = state['binding']['snapshot']['result']
        if cancelled() or result['disposition'] != 'proved-pending-replay':
            return self._report(state, lifecycle='cancelled' if cancelled() else 'succeeded')
        if self.config.mode != 'real':
            return self._report(state)  # Test-only evidence never promotes a claim.
        with service.lock:
            certificate = next(e.dst_id for e in service.view.edges_from(run, 'PRODUCED_CERTIFICATE'))
            certificate_status = service.view.node(certificate).fields['status']
        if certificate_status == 'replay-pending':
            if state['phase'] == 'replay-started' and 'receipt' not in state:
                raise ReplayReconciliationRequired('replay delivery is uncertain; reconcile provider receipts before authorizing another replay')
            if 'replay_lease' not in state:
                with service.lock:
                    checker = service.store.issue_lease(proof, f'{proof}/replay-{operation}', actor='omega-independent-replayer',
                        worker_class='replayer', selected_move_id=certificate, ttl_seconds=self.config.replay_wall_clock_ms / 1000 + 120)
                    state['replay_lease'] = checker.to_dict()
            independent = self.client()
            independent.restore(state['binding'], refresh=False)
            _, canonical = independent.cached_evidence(run)
            if 'receipt' not in state:
                state['phase'] = 'replay-started'
                self._save(operation, state)
                try:
                    state['receipt'] = independent.replay_attestation(canonical['certificate'], result['environment_hash'],
                        max_steps=state['replay_steps'], wall_clock_ms=state['replay_wall_clock_ms'])
                except (FormalV2Error, OSError) as error:
                    raise ReplayReconciliationRequired('replay delivery is uncertain') from error
                self._save(operation, state)
            with service.lock:
                if state['receipt']['status'] == 'inconclusive':
                    state['receipt_artifact'] = service.artifacts.put(canonical_json(state['receipt']))
                    self._release(SimpleNamespace(**state['replay_lease']))
                    self._save(operation, state)
                    return self._report(state)
                evidence = _Evidence(state['binding'], canonical, {}, state['receipt'])
                self._accepted(admit_replay(evidence, SimpleNamespace(**state['replay_lease']), service.gate,
                    service.artifacts, certificate_id=certificate, max_steps=state['replay_steps'],
                    wall_clock_ms=state['replay_wall_clock_ms']))
                service.catch_up(proof)
                self._release(SimpleNamespace(**state['replay_lease']))
                state['phase'] = 'replay-admitted'
                self._save(operation, state)
        if cancelled():
            return self._report(state, lifecycle='cancelled')
        with service.lock:
            if service.view.node(certificate).fields['status'] == 'replay-accepted' and service.view.node(claim).fields['status'] != 'lean-verified':
                coordinator = service.store.issue_lease(proof, f'{proof}/promote-{operation}', actor='omega-coordinator',
                    worker_class='coordinator', selected_move_id=claim)
                try:
                    self._accepted(admit_promotion(coordinator, service.gate, claim_id=claim, certificate_id=certificate))
                    service.catch_up(proof)
                finally:
                    self._release(coordinator)
            state['claim_status'] = service.view.node(claim).fields['status']
            state['phase'] = 'completed'
            self._save(operation, state)
        return self._report(state)

    def _report(self, state, lifecycle='succeeded'):
        snapshot = state.get('binding', {}).get('snapshot', {})
        disposition = (snapshot.get('result') or {}).get('disposition')
        if state.get('claim_status') == 'lean-verified':
            disposition = 'lean-verified'
        return {'proof_id': state['proof_id'], 'run_id': state['run_id'], 'phase': state['phase'],
            'claim_id': state['claim_id'],
            'lifecycle': lifecycle, 'mathematical_disposition': disposition,
            'claim_status': state.get('claim_status'),
            **({'selected_move_id': state['selected_move_id']} if 'selected_move_id' in state else {}),
            'budget': snapshot.get('budget'), 'replay_status': state.get('receipt', {}).get('status'),
            'component_mode': 'formal-v2-' + self.config.mode}
