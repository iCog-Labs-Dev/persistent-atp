"""Model-driven acceptance over the real receiver and a host-reviewed declaration.

The review file is trusted host input, never an Omega tool argument. The test
observes authoritative state independently of the model's completion message.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from commit_gate.canon import canonical_json
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from persistent_atp_client import AsyncClient, SyncClient
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.codecs import to_dict

from .api import serve
from .cli import load_config
from .export import check_export
from .formal_runtime import FormalProviderConfig
from .playground import Docker, MEMORY, Session, command_records, free_port, provider_model, save, utcnow
from .receiver import ReferenceReceiver
from .service import OperationService
from .specification import parse_json, validate_spec

STEPS = ('create', 'search', 'status', 'audit', 'export', 'restart')
TOOLS = dict(zip(STEPS, ('proof-create', 'formal-search', 'proof-status', 'proof-audit', 'proof-export', 'proof-status')))


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def notification_ids(history):
    """Read rendered bridge records, excluding quoted model tool arguments."""
    found = []
    for line in history.splitlines():
        if line.startswith('PERSISTENT_ATP_RESULT '):
            record, _ = json.JSONDecoder().raw_decode(line.removeprefix('PERSISTENT_ATP_RESULT '))
            if record.get('notification_id'):
                found.append(record['notification_id'])
    return found


def load_input(path):
    path = Path(path).resolve()
    value = parse_json(path.read_text())
    if not isinstance(value, dict) or set(value) != {'spec', 'declaration', 'review'}:
        raise ValueError('ATP input requires spec, declaration and review')
    spec = validate_spec(value['spec'])
    declaration, review = value['declaration'], value['review']
    if not isinstance(declaration, dict) or set(declaration) != {'source', 'lean_name', 'lean_type', 'toolchain'}:
        raise ValueError('declaration requires source, lean_name, lean_type and toolchain')
    if any(not isinstance(declaration[k], str) or not declaration[k].strip() for k in declaration):
        raise ValueError('declaration entries must be nonblank strings')
    source_path = (path.parent / declaration['source']).resolve()
    if source_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Lean source exceeds the provider upload bound')
    source = source_path.read_bytes()
    source.decode('utf-8')
    expected = {'verdict': 'aligned', 'claim_text': spec['theorem'],
        'lean_name': declaration['lean_name'], 'lean_type': declaration['lean_type'],
        'toolchain': declaration['toolchain'],
        'source_hash': digest(source), 'spec_hash': digest(canonical_json(spec))}
    if not isinstance(review, dict) or any(review.get(k) != v for k, v in expected.items()):
        raise ValueError('host review does not bind the exact theorem specification and declaration')
    if any(not isinstance(review.get(k), str) or not review[k].strip()
           for k in ('reviewer', 'environment_hash', 'rationale')):
        raise ValueError('host review needs reviewer, environment_hash and rationale')
    return {'spec': spec, 'declaration': deepcopy(declaration), 'review': deepcopy(review), 'source': source}


class ReceiverHost:
    """Owned HTTP receiver; graph/journal and PostgreSQL artifacts survive restart."""
    def __init__(self, config_name, output, prepared):
        self.config, self.root = load_config(config_name)
        if self.config.get('profile') != 'production' or not self.config.get('formal_provider'):
            raise ValueError('ATP playground requires a production config with formal_provider enabled')
        if self.config.get('host') not in {'127.0.0.1', 'localhost'}:
            raise ValueError('playground receiver must bind loopback')
        if not os.environ.get('ARTIFACT_DB_URL') or not os.environ.get('MORK_LIBRARY'):
            raise ValueError('ARTIFACT_DB_URL and preloaded MORK_LIBRARY are required')
        self.formal_config = FormalProviderConfig.from_environment()
        self.prepared, self.output = prepared, output
        self.workspace = self.config['workspace']
        self.token, self.port = secrets.token_urlsafe(32), int(self.config.get('port', 0))
        self.service = self.receiver = self.server = self.thread = None
        self.root.mkdir(parents=True, exist_ok=True)
        self._ownership = (self.root / 'omega-playground.lock').open('a')
        try:
            fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._ownership.close()
            raise ValueError('another playground owns this receiver root') from exc
        try:
            self.start()
            _, _, self.deployment = self.service.formal.deployment(self.service.formal.client())
            if self.deployment['environment_hash'] != prepared['review']['environment_hash']:
                raise ValueError('host review pins another provider environment')
        except BaseException:
            self.close()
            raise

    def start(self):
        self.service = OperationService(self.root, workspace=self.workspace, profile='production', formal_config=self.formal_config)
        try:
            self.receiver = ReferenceReceiver(self.service)
            self.server = serve(self.service, self.token, '127.0.0.1', self.port, receiver=self.receiver)
            self.port = self.server.server_port
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    @property
    def environment(self):
        return {'PERSISTENT_ATP_OMEGA_URL': f'http://127.0.0.1:{self.port}',
            'PERSISTENT_ATP_OMEGA_TOKEN': self.token, 'PERSISTENT_ATP_OMEGA_WORKSPACE': self.workspace}

    def close(self, *, release=True):
        if self.server:
            self.server.shutdown()
            if self.thread:
                self.thread.join(timeout=10)
            self.server.server_close()
            self.server = None
        if self.receiver:
            self.receiver.close()
            self.receiver = None
        if self.service:
            self.service.close()
            self.service = None
        if release and getattr(self, '_ownership', None):
            self._ownership.close()

    def restart(self):
        self.close(release=False)
        self.start()

    def operations(self):
        with self.receiver._lock:
            rows = self.receiver._db.execute('SELECT * FROM requests WHERE principal=? AND workspace_id=? ORDER BY rowid',
                (self.receiver.principal, self.workspace)).fetchall()
        return [dict(row) | {'body': parse_json(row['body'].decode() if isinstance(row['body'], bytes) else row['body']),
            'result': json.loads(row['result']) if row['result'] else None} for row in rows]

    def settled(self, previous, kind, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = [r for r in self.operations() if r['operation_id'] not in previous and r['body']['action']['kind'] == kind]
            if rows:
                if len(rows) != 1:
                    raise ValueError(f'{kind} admitted multiple operations for one task')
                row = rows[0]
                if row['lifecycle'] == 'succeeded':
                    return row
                if row['lifecycle'] in {'failed', 'cancelled', 'rejected'}:
                    raise ValueError(f'{kind} did not succeed: {row["result"]}')
            time.sleep(.1)
        raise TimeoutError(f'no settled {kind} operation from the model tool call')

    def prepare_declaration(self, proof):
        service, value = self.service, self.prepared
        claim = proof + '/c-1'
        with service.lock:
            node = service.view.node(claim)
            if not node or node.fields.get('claim_text') != value['spec']['theorem']:
                raise ValueError('model-created claim differs from the reviewed literal theorem')
            spec = json.loads(service.artifacts.get(node.fields['theorem_spec_artifact']))
            if spec != value['spec']:
                raise ValueError('model changed the reviewed theorem specification')
            source = service.artifacts.put(value['source'])
            review = service.artifacts.put(canonical_json(value['review']))
            actor = value['review']['reviewer']
            lease = service.store.issue_lease(proof, proof + '/intake-' + uuid.uuid4().hex,
                actor=actor, worker_class='coordinator', selected_move_id=claim)
            try:
                ops = (UpsertNode('FormalDeclaration', proof + '/fd-1', {'status': 'aligned', 'claim_id': claim,
                    'lean_type': value['declaration']['lean_type'], 'lean_name': value['declaration']['lean_name'],
                    'lean_source_artifact': source}),
                    UpsertNode('Environment', proof + '/env-1', {'toolchain': value['declaration']['toolchain'],
                        'environment_hash': self.deployment['environment_hash'],
                        'provider_environment_id': self.deployment['environment_id']}),
                    UpsertNode('Alignment', proof + '/al-1', {'status': 'aligned', 'lifecycle': 'reviewed',
                        'verdict': 'aligned', 'actor': actor, 'report_artifact': review}),
                    AddEdge('PINNED_ENVIRONMENT', proof + '/fd-1', proof + '/env-1', proof + '/intake-pin'),
                    AddEdge('ALIGNS_CLAIM', proof + '/al-1', claim, proof + '/intake-claim'),
                    AddEdge('ALIGNS_DECLARATION', proof + '/al-1', proof + '/fd-1', proof + '/intake-declaration'))
                result = service.gate.commit(Proposal(proof, actor, 'coordinator', ops=ops,
                    base_revision=lease.base_revision, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
                    selected_move_id=claim))
                if not result.accepted:
                    raise ValueError('gate rejected trusted declaration intake: ' + str(result.to_dict()))
                service.catch_up(proof)
            finally:
                service.store.release_lease(proof, lease.lease_id, fencing_token=lease.fencing_token)

    def evidence(self, proof):
        with self.service.lock:
            status = self.service._status(proof)
            count = self.service.store.verify_chain(proof)
            certificates = self.service.view.list_nodes(proof, 'Certificate')
            if not certificates:
                raise ValueError('no committed formal certificate')
            refs = self.service._artifact_pointers(proof)
            for ref in refs:
                if digest(self.service.artifacts.get(ref)) != ref:
                    raise ValueError('artifact content address mismatch')
            replays = []
            for certificate in certificates:
                for edge in self.service.view.edges_from(certificate.node_id, 'REPLAYED_BY'):
                    replay = self.service.view.node(edge.dst_id)
                    raw = self.service.artifacts.get(replay.fields['receipt_artifact'])
                    receipt = json.loads(raw)
                    if (replay.fields['actor'] == certificate.fields['actor']
                            or receipt['status'] != 'verified'
                            or receipt['verification_cost'].get('sorry_detected') is not False
                            or receipt['certificate_hash'] != certificate.fields['artifact_hash']
                            or receipt['environment_hash'] != certificate.fields['environment_hash']
                            or receipt['run_id'] != certificate.fields['producer_run_id']
                            or any(receipt[k] != certificate.fields[k] for k in ('input_artifact_hash', 'source_artifact_hash'))):
                        raise ValueError('independent replay evidence is invalid')
                    replays.append({'id': replay.node_id, 'receipt': receipt, 'artifact': digest(raw)})
            if not replays:
                raise ValueError('no independently replayed committed evidence')
            return {'status': status, 'chain_events': count, 'artifacts': refs,
                'replays': replays,
                'certificates': [{'id': c.node_id, 'fields': dict(c.fields)} for c in certificates]}


def run(args):
    if args.list:
        print(json.dumps([{'id': 'atp_' + step, 'tool': TOOLS[step]} for step in STEPS], indent=2))
        return 0
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]
    output = (Path(args.output) / run_id).resolve()
    output.mkdir(parents=True)
    report = {'run_id': run_id, 'started_at': utcnow(), 'mode': 'atp-prepared-declaration',
        'provider': args.provider, 'model': provider_model(args.provider, args.model),
        'results': [], 'output': str(output), 'review_mode': 'explicit trusted host review; no automatic formalization'}
    runner = host = session = client = None
    def event(name, **fields):
        print(json.dumps({'event': name, **fields}), flush=True)
    try:
        prepared = load_input(args.atp_input)
        host = ReceiverHost(args.atp_config, output, prepared)
        report.update(receiver_root=str(host.root), workspace=host.workspace, deployment=host.deployment)
        runner = Docker(args.image, args.provider, report['model'], output, run_id, free_port(), atp=host.environment)
        if args.build_image:
            event('building_image', image=args.image)
            runner.build(args.omega_source, args.base_image)
        session = Session(runner.ws_port, event)
        runner.start()
        report.update(image=runner.image, container=runner.name, volume=runner.volume)
        session.wait(args.startup_timeout)
        session.ping()
        client = SyncClient(AsyncClient(HTTPReceiverAdapter(host.environment['PERSISTENT_ATP_OMEGA_URL'], host.token)))
        proof = None
        search_row = before_restart = None
        for step in STEPS:
            tag = 'CASE_' + run_id + '_atp_' + step
            if step == 'create':
                instruction = 'Invoke proof-create exactly once with workspace ' + host.workspace + ' and this exact JSON spec: ' + json.dumps(prepared['spec'], ensure_ascii=False)
            elif step == 'search':
                host.prepare_declaration(proof)
                budget = {'max_steps': host.formal_config.max_steps, 'wall_clock_ms': host.formal_config.wall_clock_ms}
                instruction = f'Invoke formal-search exactly once with proof {proof}, claim {proof}/c-1, workspace {host.workspace}, budget JSON {json.dumps(budget)}. Report admission accurately; verification arrives asynchronously.'
            elif step == 'restart':
                before_restart = host.evidence(proof)
                cache_before = json.loads(runner.read(MEMORY + '/persistent_atp/workflow.json'))
                identities = [(r['request_id'], r['operation_id']) for r in host.operations()]
                (output / 'before-recreation.log').write_text(runner.logs())
                runner.remove()
                host.restart()
                if [(r['request_id'], r['operation_id']) for r in host.operations()] != identities:
                    raise ValueError('receiver changed admitted operation identities after restart')
                previous = session.connections
                old = runner.container_id
                runner.start()
                session.wait(args.startup_timeout, after=previous)
                if runner.container_id == old:
                    raise ValueError('container recreation did not create a fresh process')
                instruction = f'After restart, invoke proof-status for proof {proof} in workspace {host.workspace}. Report the literal target, proof ID, and exact claim_status. Do not create a proof or start search again.'
            elif step == 'export':
                instruction = f'Invoke proof-export for proof {proof}, destination default, workspace {host.workspace}. Report the actual export artifact hash from the completed receiver result.'
            else:
                instruction = f'Invoke {TOOLS[step]} for proof {proof} in workspace {host.workspace}. Report the actual receiver result, literal target, proof ID, and exact claim_status.'
            query = instruction + f' Use only the named persistent-ATP tool; do not use shell, metta, Python, or direct HTTP. Do not invent tool results. Tool results arrive as PERSISTENT_ATP_RESULT records in later iterations. When reporting this task, use send with a reply starting {tag}. Stop after the reply.'
            row = {'id': 'atp_' + step, 'query': query, 'response': None, 'response_source': 'omega-agent', 'started_at': utcnow()}
            event('case_started', id=row['id'])
            old_operations = {r['operation_id'] for r in host.operations()}
            since = utcnow()
            try:
                row['response'] = session.ask(query, tag, args.timeout)
                logs = runner.logs(since)
                records = command_records(logs)
                checks = {'actual_tool_call': any(r['tool'] == TOOLS[step] for r in records)}
                operation = None
                if step in {'create', 'search', 'audit', 'export'}:
                    operation = host.settled(old_operations, TOOLS[step], args.timeout)
                    row['operation'] = {k: operation[k] for k in ('request_id', 'operation_id', 'lifecycle', 'observation_version', 'result')}
                if step == 'create':
                    proof = operation['result']['proof_id']
                    actual = client.status(workspace_id=host.workspace, proof_id=proof)
                    checks['literal_target_preserved'] = actual.literal_target == prepared['spec']['theorem']
                    checks['creation_not_verification'] = not actual.formal_verified
                else:
                    evidence = host.evidence(proof)
                    row['authoritative_evidence'] = evidence
                    checks.update(verified_claim=evidence['status']['formal_verified'],
                        projection_caught_up=evidence['status']['projection_lag'] == 0,
                        journal_intact=evidence['chain_events'] == evidence['status']['revision'],
                        independently_replayed=all(c['fields']['status'] == 'replay-accepted' for c in evidence['certificates']))
                    if step == 'search':
                        search_row = operation
                        checks['verified_operation_result'] = operation['result'].get('mathematical_disposition') == 'lean-verified'
                    if step in {'status', 'restart'}:
                        checks['model_reports_verification'] = 'lean-verified' in row['response'].lower() and proof in row['response']
                    if step == 'audit':
                        checks['audit_clean'] = operation['result'].get('audit_status') == 'ok' and not operation['result'].get('missing_artifacts')
                    if step == 'export':
                        address = operation['result']['export_artifact']
                        from persistent_atp_client import ArtifactRef
                        payload = client.get_artifact(ArtifactRef(address), workspace_id=host.workspace)
                        checks['download_hash_valid'] = digest(payload) == address
                        verified = check_export(payload)
                        checks['export_covers_committed_revision'] = verified['revision'] == evidence['status']['revision']
                        checks['export_contains_formal_evidence'] = set(evidence['artifacts']) <= set(json.loads(payload)['artifacts'])
                        row['export_check'] = verified
                        (output / 'proof-export.json').write_bytes(payload)
                    if step == 'restart':
                        checks['same_committed_state'] = evidence == before_restart
                        current = [r for r in host.operations() if r['body']['action']['kind'] == 'formal-search' and r['result'] and r['result'].get('proof_id') == proof]
                        checks['search_not_duplicated'] = len(current) == 1 and current[0]['operation_id'] == search_row['operation_id']
                        cache_after = json.loads(runner.read(MEMORY + '/persistent_atp/workflow.json'))
                        checks['prepared_requests_preserved'] = all(
                            all(cache_after['intents'].get(key, {}).get(field) == value.get(field)
                                for field in ('request', 'ref'))
                            for key, value in cache_before['intents'].items())
                        checks['notification_acknowledgements_preserved'] = set(cache_before.get('displayed', ())) <= set(cache_after.get('displayed', ()))
                        notifications = notification_ids(runner.read(MEMORY + '/history.metta') or '')
                        checks['notification_history_not_duplicated'] = bool(notifications) and len(notifications) == len(set(notifications))
                row.update(checks=checks, status='passed' if all(checks.values()) else 'failed', command_records=records)
                (output / (row['id'] + '.log')).write_text(logs)
            except Exception as error:
                row.update(status='failed', reason=runner.scrub(str(error)))
                (output / (row['id'] + '.log')).write_text(runner.logs())
            row['finished_at'] = utcnow()
            report['results'].append(row)
            save(output, report)
            event('case_finished', id=row['id'], status=row['status'])
            if row['status'] != 'passed':
                break
    except Exception as error:
        report['setup_error'] = runner.scrub(str(error)) if runner else type(error).__name__ + ': ATP setup failed; check trusted configuration'
        event('setup_failed', reason=report['setup_error'])
    finally:
        if runner and runner.owned:
            (output / 'container.log').write_text(runner.logs())
            (output / 'history.metta').write_text(runner.read(MEMORY + '/history.metta') or '')
            if not args.keep_container:
                runner.remove()
        if session:
            (output / 'frames.json').write_text(json.dumps(session.frames, indent=2, ensure_ascii=False))
            session.close()
        if client:
            client.close()
        if host:
            host.close()
        recorded = {r['id'] for r in report['results']}
        for step in STEPS:
            if 'atp_' + step not in recorded:
                report['results'].append({'id': 'atp_' + step, 'query': 'Invoke ' + TOOLS[step], 'response': None,
                    'status': 'not_tested', 'reason': 'Prior acceptance step failed; inspect recorded evidence.'})
        report['finished_at'] = utcnow()
        report['summary'] = {status: sum(r['status'] == status for r in report['results']) for status in ('passed', 'failed', 'not_tested')}
        save(output, report)
        event('finished', output=str(output), **report['summary'])
    return int(bool(report.get('setup_error') or report['summary']['failed'] or report['summary']['not_tested']))
