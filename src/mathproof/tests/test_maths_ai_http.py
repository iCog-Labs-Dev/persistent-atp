"""V2 interoperability and failure gates against the pinned provider contract.

Recorded examples exercise the wire contract. Locally constructed canonical
artifacts exercise copying, corruption and restart; no Lean verdict is claimed.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from commit_gate.canon import canonical_json, content_hash
from mathproof.formal_v2 import address, validate, without_nulls
from mathproof.maths_ai_http import MathsAIAPIError, MathsAIHTTPFormalATP
from mathproof.schemas import validate as validate_canonical
from mathproof.soundness import validate_formal_search_result


EXAMPLES_DIR = Path(__file__).resolve().parents[3] / 'fixtures/formal_v2/examples'


def example(name):
    return json.loads((EXAMPLES_DIR / (name + '.json')).read_bytes())


class MemoryArtifacts:
    def __init__(self):
        self.blobs = {}

    def put(self, raw):
        self.blobs[address(raw)] = raw
        return address(raw)

    def get(self, digest):
        return self.blobs[digest]

    def exists(self, digest):
        return digest in self.blobs


def scenario(kind='goal', backend='mcts-actor-critic'):
    name = '03-start-source' if kind == 'source' else '06-start-goal'
    start = example(name)
    request, admission = start['request']['body'], start['response']['body']
    run = example('04-status-source-proved' if kind == 'source' else '08-status-goal-proved')['response']['body']
    blobs = MemoryArtifacts()
    source = b'import Mathlib\nnamespace Demo\ntheorem target : True := by trivial\nend Demo\n'
    source_hash = blobs.put(source)
    request['backend']['name'] = backend
    admission['resolved']['backend'] = run['resolved']['backend'] = backend
    run['result']['backend']['name'] = backend
    if kind == 'source':
        request['input']['artifact_hash'] = source_hash
    manifest = {'format': 'maths-ai-formal-input-v1', 'kind': kind, 'input': request['input'],
                'environment_id': request['environment_id'], 'environment_hash': request['environment_hash'],
                'source_artifact_hash': source_hash, 'source_origin': run['source_origin']}
    manifest_hash = blobs.put(canonical_json(manifest))
    cert = {'format': 'maths-ai-mcts-certificate-v1' if backend == 'mcts-actor-critic' else 'maths-ai-accepted-certificate-v1',
            'run_id': request['run_id'], 'attempt': 1, 'backend': backend,
            'input_artifact_hash': manifest_hash, 'source_artifact_hash': source_hash,
            'environment_hash': request['environment_hash'], 'proof_trace': {'tactic': 'trivial'}}
    cert_hash = blobs.put(canonical_json(cert))
    for data in (run, run['result']):
        data.update(input_artifact_hash=manifest_hash, source_artifact_hash=source_hash)
    run['result']['certificate']['artifact_hash'] = cert_hash
    run['result']['artifacts'] = [{'sha256': h, 'role': r} for h, r in
                                ((cert_hash, 'certificate'), (manifest_hash, 'admitted-input'), (source_hash, 'source'))]
    if kind == 'goal':
        admission.update(input_artifact_hash=manifest_hash, source_artifact_hash=source_hash)
    for data in (admission, run, run['result']):
        data['request_digest'] = content_hash(without_nulls(request))
    caps = example('01-capabilities')['response']['body']
    caps['deployments']['mcts-example']['backend'] = backend
    return request, admission, run, blobs, caps


def setup_adapter(kind='goal', backend='mcts-actor-critic'):
    request, admission, run, remote, caps = scenario(kind, backend)
    local = MemoryArtifacts()
    local.blobs.update(remote.blobs if kind == 'source' else {})
    atp = MathsAIHTTPFormalATP('http://localhost:8000', artifacts=local)
    def call(method, path, payload=None, **kwargs):
        if path == '/formal-capabilities':
            return deepcopy(caps)
        if path == '/formal-artifacts':
            return {'protocol_version': 2, 'artifact_hash': address(payload)}
        if path == '/formal-search/start':
            assert payload == request
            return deepcopy(admission)
        return deepcopy(run)
    def transport(method, path, *args, **kwargs):
        digest = path.removeprefix('/formal-artifacts/')
        return remote.get(digest), digest
    return atp, request, admission, run, remote, call, transport


def proved(kind='goal', backend='mcts-actor-critic'):
    atp, request, admission, run, remote, call, transport = setup_adapter(kind, backend)
    with patch.object(atp, '_call', side_effect=call), patch.object(atp, '_transport', side_effect=transport):
        assert atp.formal_search_start(request)['disposition'] == 'searching'
        result = atp.formal_search_status(request['run_id'])
    return atp, request, result, run, remote


def attestation(request, run, status='verified'):
    return {'protocol_version': 2, 'status': status, 'reason': None if status == 'verified' else 'verification limit or failure',
            'run_id': request['run_id'], 'certificate_hash': run['result']['certificate']['artifact_hash'],
            'input_artifact_hash': run['input_artifact_hash'], 'source_artifact_hash': run['source_artifact_hash'],
            'source_origin': run['source_origin'], 'environment_hash': request['environment_hash'],
            'backend': request['backend']['name'], 'verification_cost': {'tactic_steps': 1, 'sorry_detected': False}}


@pytest.mark.parametrize('kind,backend', [('source','mcts-actor-critic'), ('goal','mcts-actor-critic'), ('goal','accepted-gnn')])
def test_proof_copies_exact_artifacts_and_durable_binding(kind, backend):
    atp, request, result, run, remote = proved(kind, backend)
    assert result['disposition'] == 'proved-pending-replay'
    assert validate_canonical('formal-search-result', result) == []
    assert validate_formal_search_result(result) == ()
    for artifact in result['artifacts']:
        assert atp.artifacts.get(artifact['sha256']) == remote.get(artifact['sha256'])
    binding = json.loads(atp.artifacts.get(result['certificate']['binding_artifact_hash']))
    assert binding['request'] == request
    assert result['provider']['budget']['admitted_max_steps'] == 60
    assert result['provider']['frontier_resume'] is False
    assert 'checkpoint' not in result


@pytest.mark.parametrize('status,cert_status', [('verified','replay-accepted'), ('rejected','replay-rejected'), ('inconclusive','replay-pending')])
def test_three_replay_outcomes_and_restart(status, cert_status):
    atp, request, result, run, _ = proved()
    body = attestation(request, run, status)
    with patch.object(atp, '_call', return_value=body) as call:
        verdict = atp.formal_replay(result['certificate'], request['environment_hash'])
    assert verdict['status'] == status
    assert atp._latest[request['run_id']]['certificate']['status'] == cert_status
    assert validate_canonical('formal-search-result', atp._latest[request['run_id']]) == []
    assert validate_formal_search_result(atp._latest[request['run_id']]) == ()
    payload = call.call_args.args[2]
    assert validate('formal-replay-request', payload) == []
    assert 'proof_trace' not in payload
    restarted = MathsAIHTTPFormalATP('http://localhost:8000', artifacts=atp.artifacts)
    with patch.object(restarted, '_call', return_value=body):
        assert restarted.formal_replay(result['certificate'], request['environment_hash'])['status'] == status


@pytest.mark.parametrize('field', ['run_id','certificate_hash','input_artifact_hash','source_artifact_hash','environment_hash','backend','source_origin'])
def test_replay_rejects_wrong_identity(field):
    atp, request, result, run, _ = proved()
    body = attestation(request, run)
    body[field] = 'sha256:' + '00'*32 if 'hash' in field else ('accepted-gnn' if field == 'backend' else 'caller-artifact' if field == 'source_origin' else 'other/fr-1')
    with patch.object(atp, '_call', return_value=body), pytest.raises(MathsAIAPIError):
        atp.formal_replay(result['certificate'], request['environment_hash'])


def test_environment_change_is_inconclusive_and_never_accepted():
    atp, request, result, run, _ = proved()
    body = attestation(request, run, 'inconclusive')
    body['environment_hash'] = 'sha256:'+'00'*32
    with patch.object(atp, '_call', return_value=body):
        assert atp.formal_replay(result['certificate'], request['environment_hash'])['status'] == 'inconclusive'
    assert atp._latest[request['run_id']]['certificate']['status'] == 'replay-pending'


@pytest.mark.parametrize('failure', ['bytes', 'header', 'store'])
def test_artifact_failures_do_not_emit_certificate(failure):
    atp, request, admission, run, remote, call, transport = setup_adapter()
    with patch.object(atp, '_call', side_effect=call):
        atp.formal_search_start(request)
    if failure == 'store':
        atp.artifacts.put = lambda raw: (_ for _ in ()).throw(OSError('offline'))
        fn = transport
    else:
        def fn(method, path, **kwargs):
            raw, digest = transport(method, path)
            return (raw + b'tampered', digest) if failure == 'bytes' else (raw, 'wrong')
    with patch.object(atp, '_call', return_value=run), patch.object(atp, '_transport', side_effect=fn), pytest.raises(MathsAIAPIError):
        atp.formal_search_status(request['run_id'])
    assert 'certificate' not in atp._latest[request['run_id']]


@pytest.mark.parametrize('change', ['missing-child', 'failed-tactic', 'heuristic', 'environment', 'lease', 'digest', 'budget'])
def test_result_failure_gates(change):
    atp, request, admission, run, remote, call, transport = setup_adapter('source')
    with patch.object(atp, '_call', side_effect=call):
        atp.formal_search_start(request)
    result = run['result']
    if change == 'missing-child':
        result['tactic_edges'][-1]['produced_goal_ids'].pop()
    elif change == 'failed-tactic':
        result['tactic_edges'][0]['executor_result'] = 'timeout'
    elif change == 'heuristic':
        result['tactic_edges'][0]['tactic_label'] = 'PLN_fallback'
    elif change == 'environment':
        result['environment_hash'] = 'sha256:'+'00'*32
    elif change == 'lease':
        result['coordinator_binding']['fencing_token'] += 1
    elif change == 'digest':
        run['request_digest'] = 'sha256:'+'00'*32
    else:
        run['budget']['remaining_steps'] += 1
    with patch.object(atp, '_call', return_value=run), patch.object(atp, '_transport', side_effect=transport), pytest.raises(MathsAIAPIError):
        atp.formal_search_status(request['run_id'])


@pytest.mark.parametrize('field,value', [('max_steps', True), ('max_steps', 1.0)])
def test_strict_budget_numbers(field, value):
    atp, request, *_ = setup_adapter()
    request['budget'][field] = value
    with pytest.raises(MathsAIAPIError):
        atp.formal_search_start(request)


def test_removed_field_and_legacy_request_are_refused():
    atp, request, *_ = setup_adapter()
    request['search_policy']['max_tactic_steps'] = 10
    with pytest.raises(MathsAIAPIError):
        atp.formal_search_start(request)
    from mathproof.tests.test_maths_ai_adapter import REQUEST
    with pytest.raises(MathsAIAPIError):
        atp.formal_search_start(REQUEST)


def test_source_is_never_changed_to_goal():
    atp, request, *_ = setup_adapter('source')
    with patch.object(atp, 'formal_capabilities', return_value=example('01-capabilities')['response']['body']), patch.object(atp, '_call') as call:
        atp.artifacts.blobs.clear()
        with pytest.raises(MathsAIAPIError):
            atp.formal_search_start(request)
        call.assert_not_called()


def test_capabilities_block_unsupported_backend_input_and_environment():
    for change in ('source', 'universe', 'environment'):
        atp, request, admission, run, remote, call, transport = setup_adapter('source' if change == 'source' else 'goal')
        cap = example('01-capabilities')['response']['body']
        if change == 'source':
            cap['deployments']['mcts-example']['inputs'] = ['goal']
        elif change == 'universe':
            request['backend']['name'] = cap['deployments']['mcts-example']['backend'] = 'accepted-gnn'
            request['input']['universe_levels'] = ['u']
        else:
            request['environment_hash'] = 'sha256:'+'00'*32
        with patch.object(atp, 'formal_capabilities', return_value=cap), pytest.raises(MathsAIAPIError):
            atp.formal_search_start(request)


def test_lost_start_reply_recovers_only_same_bound_run():
    atp, request, admission, run, remote, call, transport = setup_adapter()
    def lost(method, path, payload=None, **kwargs):
        if path == '/formal-search/start':
            raise MathsAIAPIError('connection lost')
        return call(method, path, payload, **kwargs)
    with patch.object(atp, '_call', side_effect=lost) as calls, patch.object(atp, '_transport', side_effect=transport):
        assert atp.formal_search_start(request)['certificate']['status'] == 'replay-pending'
    assert sum(c.args[1] == '/formal-search/start' for c in calls.call_args_list) == 1


def test_retry_and_cancel_from_recorded_examples_without_frontier_claim():
    atp, request, *_ = setup_adapter()
    original = deepcopy(request)
    original['run_id'] = 'example/fr-3'
    original['input']['goal_text'] = 'uses:40'
    # Recreate the request whose digest the recorded run echoes.
    first = example('11-status-stagnated')['response']['body']
    original['backend']['seed'] = 42
    original['budget'] = {'max_steps':100, 'wall_clock_ms':45000}
    first['request_digest'] = content_hash(without_nulls(original))
    first['result']['request_digest'] = first['request_digest']
    second = example('12-retry')['response']['body']
    second['request_digest'] = first['request_digest']
    atp._requests[original['run_id']] = original
    with patch.object(atp, '_evidence'), patch.object(atp, '_call', side_effect=[first, second]) as calls:
        result = atp.formal_search_resume(original['run_id'])
    assert result['disposition'] == 'searching'
    assert result['provider']['budget']['remaining_steps'] == 20
    assert calls.call_args_list[-1].args[1].endswith('/retry')
    with pytest.raises(MathsAIAPIError):
        atp.formal_search_resume(original['run_id'], {'max_steps':1000})
    cancel = example('16-cancel')['response']['body']
    original['run_id'] = cancel['run_id']
    cancel['request_digest'] = content_hash(without_nulls(original))
    atp._requests[original['run_id']] = original
    with patch.object(atp, '_call', return_value=cancel):
        result = atp.formal_search_cancel(original['run_id'])
    assert result['disposition'] == 'searching'
    assert result['provider']['status'] == 'cancelling'


def test_real_http_transport_uses_binary_artifacts_and_v2_routes():
    request, admission, run, remote, caps = scenario('source')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): self.handle_call()
        def do_POST(self): self.handle_call()
        def handle_call(self):
            data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            calls.append((self.command, self.path, data))
            headers = {}
            if self.path == '/formal-capabilities': body, status = canonical_json(caps), 200
            elif self.path == '/formal-artifacts': body, status = canonical_json({'protocol_version':2,'artifact_hash':address(data)}), 201
            elif self.path.startswith('/formal-artifacts/'):
                digest = self.path.split('/formal-artifacts/')[1]
                body, status = remote.get(digest), 200
                headers['X-Content-Hash'] = digest
            elif self.path == '/formal-search/start': body, status = canonical_json(admission), 202
            elif self.path == '/formal-replay': body, status = canonical_json(attestation(request, run)), 200
            else: body, status = canonical_json(run), 200
            self.send_response(status)
            for k,v in headers.items(): self.send_header(k,v)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    local = MemoryArtifacts(); local.blobs.update(remote.blobs)
    try:
        atp = MathsAIHTTPFormalATP(f'http://127.0.0.1:{server.server_port}', artifacts=local)
        atp.formal_search_start(request)
        result = atp.formal_search_status(request['run_id'])
        assert atp.formal_replay(result['certificate'], request['environment_hash'])['status'] == 'verified'
    finally:
        server.shutdown(); server.server_close(); thread.join()
    assert any(p == '/formal-search/example/fr-1' for _,p,_ in calls)
    assert next(raw for _,p,raw in calls if p == '/formal-artifacts').startswith(b'import Mathlib')


def rebind_graph_ids(result, run_id, attempt):
    """Give a constructed graph the provider coordinates of its test run."""
    def identity(value):
        return f"{run_id}/attempt-{attempt}/{value.rsplit('/', 1)[-1]}"
    result['root_state_id'] = identity(result['root_state_id'])
    for state in result['states']:
        state['state_id'] = identity(state['state_id'])
    for edge in result['tactic_edges']:
        edge['tactic_id'] = identity(edge['tactic_id'])
        edge['source_state_id'] = identity(edge['source_state_id'])
        edge['produced_goal_ids'] = [identity(i) for i in edge['produced_goal_ids']]


def test_provider_local_graph_ids_are_stable_and_disjoint_between_runs_and_attempts():
    atp, request, result, run, _ = proved('source')
    baseline = deepcopy(run['result'])
    other = deepcopy(request); other['run_id'] = 'example/fr-2'
    first, second, third = deepcopy(baseline), deepcopy(baseline), deepcopy(baseline)
    rebind_graph_ids(second, other['run_id'], 1)
    rebind_graph_ids(third, request['run_id'], 2)
    atp._scope_graph(first, request, 1)
    atp._scope_graph(second, other, 1)
    atp._scope_graph(third, request, 2)
    sets = [{s['state_id'] for s in r['states']} for r in (first,second,third)]
    assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
    assert sets[0] == {s['state_id'] for s in result['states']}
    assert validate_canonical('formal-search-result', result) == []


def test_poll_preserves_independent_replay_verdict():
    atp, request, result, run, remote = proved('source')
    with patch.object(atp, '_call', return_value=attestation(request,run)):
        atp.formal_replay(result['certificate'], request['environment_hash'])
    with patch.object(atp, '_call', return_value=run), patch.object(atp,'_fetch_artifact', side_effect=remote.get):
        polled = atp.formal_search_status(request['run_id'])
    assert polled['certificate']['status'] == 'replay-accepted'


def test_http_409_is_transport_error_with_typed_category():
    from io import BytesIO
    from urllib.error import HTTPError
    atp, request, result, run, _ = proved()
    error = example('20-replay-binding-mismatch')['response']['body']
    exc = HTTPError('http://localhost/formal-replay',409,'conflict',{},BytesIO(canonical_json(error)))
    with patch('mathproof.maths_ai_http.urlopen', side_effect=exc), pytest.raises(MathsAIAPIError) as caught:
        atp.formal_replay(result['certificate'],request['environment_hash'])
    assert caught.value.code == 'certificate-hash-mismatch'
    assert atp._latest[request['run_id']]['certificate']['status'] == 'replay-pending'


def test_inconclusive_without_a_current_environment_is_representable():
    atp, request, result, run, _ = proved()
    body = attestation(request,run,'inconclusive'); body['environment_hash'] = ''
    with patch.object(atp,'_call',return_value=body):
        atp.formal_replay(result['certificate'],request['environment_hash'])
    assert validate_canonical('formal-search-result',atp._latest[request['run_id']]) == []
    assert atp._latest[request['run_id']]['certificate']['status'] == 'replay-pending'


@pytest.mark.parametrize('name', ['13-status-budget-exhausted','15-status-candidate-not-proof',
                                  '17-status-cancelled','29-status-source-rejected','30-status-artifact-missing'])
def test_recorded_terminal_nonproof_outcomes_stay_nonproof(name):
    run = example(name)['response']['body']
    request = example('03-start-source' if run['input_kind']=='source' else '06-start-goal')['request']['body']
    request['run_id'] = run['run_id']
    # The selected example binds a different input; preserve the real response
    # structure and recompute only the caller identity digest for this fixture.
    run['request_digest'] = run['result']['request_digest'] = content_hash(without_nulls(request))
    atp = MathsAIHTTPFormalATP('http://localhost:8000',artifacts=MemoryArtifacts())
    with patch.object(atp,'_evidence'):
        result = atp._translate(run,request)
    assert result['disposition'] == run['disposition']
    assert 'certificate' not in result
    assert 'checkpoint' not in result


@pytest.mark.parametrize('coordinate', ['run', 'attempt'])
@pytest.mark.parametrize('kind', ['state', 'tactic'])
def test_provider_graph_cannot_belong_to_another_run_or_attempt(coordinate, kind):
    atp, request, admission, run, remote, call, transport = setup_adapter('source')
    with patch.object(atp, '_call', side_effect=call):
        atp.formal_search_start(request)
    result = run['result']
    other_run = 'example/fr-2' if coordinate == 'run' else request['run_id']
    other_attempt = 2 if coordinate == 'attempt' else run['attempts']
    if kind == 'state':
        rebind_graph_ids(result, other_run, other_attempt)
    else:
        for edge in result['tactic_edges']:
            local = edge['tactic_id'].rsplit('/', 1)[-1]
            edge['tactic_id'] = f'{other_run}/attempt-{other_attempt}/{local}'
    # Shape validation alone accepts these ids. They must also match this run.
    assert validate('formal-run', run) == []
    with patch.object(atp, '_call', return_value=run), patch.object(atp, '_transport', side_effect=transport):
        with pytest.raises(MathsAIAPIError, match='run and attempt'):
            atp.formal_search_status(request['run_id'])
    assert 'certificate' not in atp._latest[request['run_id']]


def test_restoring_run_after_replay_preserves_certificate_binding_hash():
    atp, request, original, run, remote = proved('source')
    # A real provider adds these summaries after independent replay. That
    # mutable history must not change an immutable certificate binding.
    run['replays'] = [{'replay': 1, 'status': 'verified'}]
    assert validate('formal-run', run) == []
    restarted = MathsAIHTTPFormalATP('http://localhost:8000', artifacts=atp.artifacts)
    with patch.object(restarted, '_call', return_value=run), patch.object(restarted, '_fetch_artifact', side_effect=remote.get):
        restored = restarted.restore_run(request)
    assert restored['certificate']['artifact_hash'] == original['certificate']['artifact_hash']
    assert restored['certificate']['binding_artifact_hash'] == original['certificate']['binding_artifact_hash']
    assert restarted._snapshots[request['run_id']]['replays'] == run['replays']
