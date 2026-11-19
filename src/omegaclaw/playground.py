"""Live, standalone Omega capability playground with query/response evidence."""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

from dotenv import load_dotenv


MEMORY = '/PeTTa/repos/Omega/memory'
PROVIDERS = {'DeepSeek': ('DEEPSEEK_API_KEY', 'deepseek-flash'),
             'ASIOne': ('ASIONE_API_KEY', 'asi1-ultra')}
DEPENDENCIES = {'file_read': ('file_write',), 'file_append': ('file_write',),
    'file_base64': ('file_write',), 'file_delete': ('file_base64',),
    'working_recall': ('pin',), 'query': ('remember',), 'episodes': ('remember',),
    'restart': ('remember',), 'workflow': ('file_write',), 'git': ('file_write',)}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def provider_model(provider, override=None):
    setting = 'ASIONE_MODEL' if provider == 'ASIOne' else 'DEEPSEEK_MODEL'
    return override or os.environ.get(setting) or PROVIDERS[provider][1]


def proxy_template(template, port, provider, asi_base_url=None):
    template = template.replace('listen 8080;', f'listen 127.0.0.1:{port};')
    if provider != 'ASIOne' or not asi_base_url:
        return template
    parsed = urlsplit(asi_base_url)
    try:
        invalid_port = parsed.port == 0
    except ValueError as error:
        raise ValueError('ASIONE_BASE_URL has an invalid port') from error
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or invalid_port or any(char.isspace() or char in ';{}$\"\'\\' for char in asi_base_url)
            or not (parsed.scheme == 'https' or
                    parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'})):
        raise ValueError('ASIONE_BASE_URL requires HTTPS (or loopback HTTP) without credentials, query, or fragment')
    upstream, host = 'proxy_pass https://api.asi1.ai/v1/;', 'proxy_set_header Host api.asi1.ai;'
    if template.count(upstream) != 1 or template.count(host) != 1:
        raise ValueError('image has an unexpected ASI proxy configuration')
    return template.replace(upstream, f'proxy_pass {asi_base_url.rstrip("/")}/;').replace(
        host, f'proxy_set_header Host {parsed.netloc};')


@dataclass(frozen=True)
class Case:
    name: str
    feature: str
    instruction: str
    tools: tuple[str, ...] = ()
    expected: str | None = None
    file: str | None = None
    content: str | None = None
    action: str | None = None


def cases(run_id):
    root = f'{MEMORY}/playground/{run_id}'
    value = 'azure-' + run_id
    initial = 'omega-' + run_id
    binary = 'Quotes: "hello"\nUnicode: λ\n'
    encoded = base64.b64encode(binary.encode()).decode()
    return [
        Case('generation', 'Text generation', 'Explain what a fish is in one sentence. Include the words fish and water.', ('send',)),
        Case('version', 'Native tool invocation', 'Invoke version, then report the actual returned Omega version.', ('version',)),
        Case('nop', 'NOP tool', 'Invoke nop once, then send the completion reply. Do not repeatedly invoke nop.', ('nop',)),
        Case('shell', 'Shell computation', 'Use shell to run Python and compute 17 multiplied by 23. Report the captured result.', ('shell',), '391'),
        Case('shell_error', 'Shell error handling', 'Use shell to run python3 -c "raise RuntimeError(\'OMEGA_EXPECTED_ERROR\')". Report the actual error without claiming success.', ('shell',), 'OMEGA_EXPECTED_ERROR'),
        Case('shell_timeout', 'Shell timeout handling', 'Use shell to run sleep 10. Report whether the tool timed out; do not retry the command.', ('shell',)),
        Case('file_write', 'File creation', f'Check get-io-policy. Use shell only to create directory {root}. Use write-file to write exactly {initial} into {root}/note.txt, without a newline. Report completion.', ('get-io-policy', 'write-file'), file=f'{root}/note.txt', content=initial),
        Case('file_read', 'File reading', f'Use read-file to read {root}/note.txt and report its exact contents.', ('read-file',), initial, f'{root}/note.txt', initial),
        Case('file_append', 'File append', f'Use append-file to append the line -appended to {root}/note.txt using its normal line-separator behavior. Read the file and report its actual complete contents.', ('append-file', 'read-file'), '-appended', f'{root}/note.txt', initial + '\n-appended\n'),
        Case('file_base64', 'Binary-safe file writing', f'Use write-file-b64 with base64 content {encoded} to create {root}/encoded.txt. Read it and confirm the write.', ('write-file-b64',), file=f'{root}/encoded.txt', content=binary),
        Case('file_delete', 'File deletion', f'Use delete-file to remove only {root}/encoded.txt. Report whether it was removed.', ('delete-file',), file=f'{root}/encoded.txt', action='absent'),
        Case('pin', 'Working memory: store', f'Use pin to retain this working note: the playground color is {value}. Acknowledge it.', ('pin',)),
        Case('working_recall', 'Working memory: recall', 'What playground color did I ask you to pin earlier? Report its exact value.', expected=value),
        Case('remember', 'Long-term memory: store', f'Use remember to store this exact fact: Playground {run_id} has favorite color {value}. Report the stored fact only after the tool returns.', ('remember',), value),
        Case('query', 'Long-term memory: retrieval', f'Use query, not just chat history, to retrieve the favorite color for Playground {run_id}. Report the exact color from the tool result.', ('query',), value),
        Case('episodes', 'Episodic history', 'Use episodes with timestamp {episode_time} to inspect our recent playground conversation. Report the playground color from the returned history.', ('episodes',), value),
        Case('metta', 'MeTTa computation', 'Use metta to evaluate the expression (+ 17 23). Report the actual evaluated result.', ('metta',), '40'),
        Case('nal', 'NAL inference', 'Use metta to evaluate (|- ((--> robin bird) (stv 1.0 0.9)) ((--> bird animal) (stv 1.0 0.9))). Report the actual returned conclusion and truth value.', ('metta',)),
        Case('pln', 'PLN inference', 'Use metta to evaluate (|~ ((Implication (Inheritance $1 (IntSet Feathered)) (Inheritance $1 Bird)) (stv 1.0 0.9)) ((Inheritance Pingu (IntSet Feathered)) (stv 1.0 0.9))). Report the actual returned conclusion and truth value.', ('metta',)),
        Case('websearch', 'Web search', 'Use websearch to search for Python official documentation. Report one title returned by the tool. Do not invent links if the tool returns no URLs.', ('websearch',)),
        Case('workflow', 'Multi-step task', f'Create {root}/input.txt containing exactly 6,7. Use shell with Python to read that file, multiply the two numbers, and write the result into {root}/product.txt. Use read-file to inspect the output and report it.', ('shell', 'read-file'), '42', f'{root}/product.txt', '42'),
        Case('git', 'Local Git operations', f'Use shell to create a new local Git repository only at {root}/git. Configure its local user as Omega Playground with email playground@example.invalid. Create payload.txt containing exactly local-test without a newline, stage it, and commit with subject local-playground-{run_id}. Do not configure a remote or push. Report the commit subject.', ('shell',), 'local-playground-' + run_id, f'{root}/git/payload.txt', 'local-test', action='git'),
        Case('policy', 'Filesystem access policy', f'Check get-io-policy. Attempt write-file of the text DENIED into /etc/omega-playground-{run_id}.txt. Report the actual denial; do not alter the policy or try another protected path.', ('get-io-policy', 'write-file'), file=f'/etc/omega-playground-{run_id}.txt', action='denied'),
        Case('reconnect', 'WebSocket reconnect and deduplication', 'Confirm that you received this fresh request after a WebSocket reconnect. Do not repeat any earlier task.', ('send',), action='reconnect'),
        Case('malformed', 'Malformed channel frames', 'Confirm that the channel still works after the malformed test frames. Do not repeat any earlier task.', ('send',), action='malformed'),
        Case('restart', 'Container recreation and persistent memory', f'Use query to retrieve the favorite color for Playground {run_id} from long-term memory after this container was recreated. Report the exact color from retrieval.', ('query',), value, action='restart'),
    ]


class Session:
    """One pending request; reconnect replays only unfinished work."""
    def __init__(self, port, event):
        from websockets.sync.server import serve
        self.event, self.lock = event, threading.RLock()
        self.connected = threading.Condition(self.lock)
        self.ws, self.pending = None, None
        self.sequence, self.connections, self.last_seen = 0, 0, None
        self.replies, self.seen = queue.Queue(), set()
        self.frames = []
        self.server = serve(self._handler, '127.0.0.1', port, ping_interval=20,
                            ping_timeout=20, max_size=65536)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self, ws):
        try:
            frame = json.loads(ws.recv(timeout=30))
            if frame.get('type') != 'resume':
                raise ValueError('expected resume')
            last = frame.get('last_seen_seq')
            if last is not None and (type(last) is not int or last < 0):
                raise ValueError('invalid resume sequence')
            with self.connected:
                self.ws, self.last_seen = ws, last
                self.sequence = max(self.sequence, last or 0)
                self.connections += 1
                pending = self.pending
                self.connected.notify_all()
            self.event('connected', connection=self.connections, last_seen_seq=last)
            if pending and (last is None or pending['seq'] > last):
                ws.send(json.dumps(pending))
            for raw in ws:
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if frame.get('type') != 'agent_message':
                    continue
                identity, text = frame.get('client_seq'), frame.get('text')
                if not isinstance(identity, str) or not isinstance(text, str):
                    continue
                ws.send(json.dumps({'type': 'ack', 'seq': None, 'client_seq': identity}))
                with self.lock:
                    if identity in self.seen:
                        continue
                    self.seen.add(identity)
                    self.frames.append(frame)
                self.replies.put(frame)
        except Exception as error:
            self.event('connection_ended', reason=type(error).__name__)
        finally:
            with self.connected:
                if self.ws is ws:
                    self.ws = None
                self.connected.notify_all()

    def wait(self, timeout, *, after=0):
        with self.connected:
            if not self.connected.wait_for(lambda: self.ws is not None and self.connections > after, timeout):
                raise TimeoutError('Omega did not connect/resume in time')

    def ping(self):
        with self.lock:
            ws = self.ws
        if ws is None or not ws.ping().wait(timeout=10):
            raise TimeoutError('Omega did not answer WebSocket ping')
        return {'frame': 'pong', 'connection': self.connections, 'last_seen_seq': self.last_seen}

    def ask(self, text, tag, timeout):
        self.wait(timeout)
        with self.lock:
            self.sequence += 1
            frame = {'type': 'user_message', 'seq': self.sequence, 'text': text}
            self.pending = frame
            ws = self.ws
        try:
            ws.send(json.dumps(frame))
        except Exception:
            pass  # Reconnect handler retains the exact sequence/body.
        deadline = time.monotonic() + timeout
        try:
            while True:
                reply = self.replies.get(timeout=max(.01, deadline - time.monotonic()))
                if tag in reply['text']:
                    return reply['text']
                self.event('unmatched_reply', client_seq=reply['client_seq'])
                if time.monotonic() >= deadline:
                    raise TimeoutError('no correlated agent reply')
        except queue.Empty as error:
            raise TimeoutError('no correlated agent reply') from error
        finally:
            with self.lock:
                self.pending = None

    def reconnect(self, timeout):
        with self.lock:
            ws, previous, consumed = self.ws, self.connections, self.sequence
        ws.close(1012, 'playground reconnect check')
        self.wait(timeout, after=previous)
        result = self.ping()
        result['resumed_sequence_matches'] = self.last_seen == consumed
        # Deliberately replay an already consumed sequence. It must be ignored.
        self.ws.send(json.dumps({'type': 'user_message', 'seq': consumed,
            'text': 'DUPLICATE_FRAME_MUST_NOT_EXECUTE'}))
        return result

    def malformed(self):
        for raw in ('not-json', '[]', '{"type":"user_message","seq":"bad","text":17}', '{"type":"unknown"}'):
            self.ws.send(raw)

    def close(self):
        with self.lock:
            ws = self.ws
        if ws:
            ws.close()
        self.server.shutdown()
        self.thread.join(timeout=10)


class Docker:
    def __init__(self, image, provider, model, output, run_id, ws_port):
        self.image, self.provider, self.model, self.output = image, provider, model, output
        self.name, self.volume = 'omega-playground-' + run_id, 'omega-playground-memory-' + run_id
        self.ws_port, self.proxy_port = ws_port, free_port()
        self.key_name = PROVIDERS[provider][0]
        self.key = os.environ.get(self.key_name, '')
        if not self.key:
            raise ValueError(self.key_name + ' is not configured')
        self.owned = False

    def scrub(self, text):
        return text.replace(self.key, '[REDACTED]') if self.key else text

    def call(self, *args, timeout=40, required=True):
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
            timeout=timeout, env=os.environ.copy())
        result.stdout, result.stderr = self.scrub(result.stdout), self.scrub(result.stderr)
        if required and result.returncode:
            raise RuntimeError('docker ' + args[0] + ': ' + result.stderr[-1500:])
        return result

    def start(self):
        if not (self.output / 'nginx.conf.template').exists():
            self.image = self.call('image', 'inspect', self.image, '--format', '{{.Id}}').stdout.strip()
            template = self.call('run', '--rm', '--network=none', '--entrypoint=cat', self.image,
                                 '/opt/nginx/nginx.conf.template').stdout
            if 'location = /deepseek/chat/completions {' not in template and self.provider == 'DeepSeek':
                raise ValueError('image does not include the DeepSeek provider overlay')
            template = proxy_template(template, self.proxy_port, self.provider,
                os.environ.get('ASIONE_BASE_URL'))
            path = self.output / 'nginx.conf.template'
            path.write_text(template)
            path.chmod(0o644)
            self.call('volume', 'create', '--label', 'persistent-atp.omega-playground=' + self.name, self.volume)
        self.owned = True  # Cleanup checks the ownership label even after a failed start.
        result = self.call('run', '-d', '-it', '--name', self.name, '--network=host', '--init',
            '--label', 'persistent-atp.omega-playground=' + self.name,
            '--security-opt=no-new-privileges:true', '--tmpfs', '/tmp:size=64m,mode=1777,exec',
            '--tmpfs', '/var/tmp:size=64m,mode=1777,exec', '--tmpfs', '/run:size=16m,mode=755',
            '--volume', self.volume + ':' + MEMORY,
            '--mount', f'type=bind,src={self.output / "nginx.conf.template"},dst=/opt/nginx/playground-template,readonly',
            '-e', self.key_name, '-e', 'NGINX_TEMPLATE=/opt/nginx/playground-template',
            '-e', 'IMPORT_KB_ON_START=0', '-e', 'OMEGA_persistentAtpEnabled=disabled',
            self.image, 'commchannel=websocket', f'WS_URL=ws://127.0.0.1:{self.ws_port}',
            'provider=' + self.provider, 'model=' + self.model, 'embeddingprovider=Local',
            f'GATEWAY_URL=http://localhost:{self.proxy_port}', 'persistentAtpEnabled=disabled',
            'showStartupVersion=false', 'maxNewInputLoops=12', 'maxOutputToken=3000',
            'sleepInterval=1', 'wakeupInterval=3600', 'maxWakeLoops=0')
        self.container_id = result.stdout.strip()

    def build(self, source, base):
        from .installer import install, bundle_runtime
        from .runtime import verify_checkout
        source = Path(source).resolve()
        verify_checkout(source)
        derived = self.output / 'build/source/Omega'
        shutil.copytree(source, derived, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        install(derived)
        context = self.output / 'build/context'
        bundle_runtime(derived, context, base)
        result = self.call('build', '-t', self.image, str(context), timeout=300)
        (self.output / 'build.log').write_text(result.stdout + result.stderr)

    def exec(self, *args, required=True):
        return self.call('exec', '--user', '65534:65534', self.name, *args, required=required)

    def read(self, path):
        code = 'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); print(json.dumps(p.read_bytes().decode("utf-8") if p.exists() else None))'
        return json.loads(self.exec('python3', '-c', code, path).stdout)

    def logs(self, since=None):
        args = ['logs'] + (['--since', since] if since else []) + [self.name]
        result = self.call(*args, required=False)
        return result.stdout + result.stderr

    def chroma(self):
        code = '''import json,sqlite3,pathlib,os
paths=list(pathlib.Path("/PeTTa/repos/Omega/memory").rglob("chroma.sqlite3"))
paths += [pathlib.Path(os.environ.get("CHROMA_DB_PATH","/PeTTa/chroma_db"))/"chroma.sqlite3"]
out={"count":0,"documents":[],"path":None}
for p in paths:
 if not p.is_file(): continue
 try:
  c=sqlite3.connect("file:"+str(p)+"?mode=ro",uri=True)
  out={"path":str(p),"count":c.execute("select count(*) from embeddings").fetchone()[0],"documents":[]}
  out["documents"]=[r[0] for r in c.execute("select string_value from embedding_metadata where key='chroma:document'")]
  c.close(); break
 except sqlite3.Error:
  out={"count":None,"documents":[],"path":str(p),"error":"cannot inspect vector store"}; break
print(json.dumps(out))'''
        result = self.exec('python3', '-c', code, required=False)
        return json.loads(result.stdout) if result.returncode == 0 else {'count': None, 'documents': []}

    def remove(self):
        if self.owned:
            label = self.call('inspect', self.name, '--format', '{{index .Config.Labels "persistent-atp.omega-playground"}}', required=False)
            if label.returncode == 0 and label.stdout.strip() == self.name:
                self.call('rm', '-f', self.name)
            self.owned = False


def command_records(log):
    records = []
    for line in log.splitlines():
        match = re.match(r'^\d{4}-\d\d-\d\d [\d:]+ \| .*? \| loop \| \(COMMAND_RETURN: \(\(([a-z][a-z0-9-]*)(?=\s|\))', line)
        if not match:
            continue
        value = re.search(r'"SUCCESS, RETURN: ((?:[^"\\]|\\.)*)"', line)
        records.append({'tool': match.group(1), 'value': None if value is None else
            value.group(1).replace('\\n', '\n').replace('\\"', '"')})
    return records


def returned_values(log):
    return [row['value'] for row in command_records(log) if row['value'] is not None]


def evaluate(case, reply, tools, returns, *, actual_file=None, before_count=None, chroma=None, control=None, records=None):
    checks = {'nonempty_correlated_response': bool(reply and reply.strip())}
    checks.update({'tool:' + tool: tool in tools for tool in case.tools})
    joined = '\n'.join(returns)
    expected_tool = {'shell': 'shell', 'shell_error': 'shell', 'shell_timeout': 'shell',
        'metta': 'metta', 'nal': 'metta', 'pln': 'metta', 'query': 'query',
        'restart': 'query', 'episodes': 'episodes', 'websearch': 'websearch', 'policy': 'write-file'}.get(case.name)
    if records is not None and expected_tool:
        joined = '\n'.join(row['value'] for row in records if row['tool'] == expected_tool and row['value'] is not None)
    if case.expected is not None:
        checks['response_contains_expected_value'] = case.expected.lower() in (reply or '').lower()
    if case.file:
        checks['filesystem_effect'] = actual_file is None if case.action in {'absent', 'denied'} else actual_file == case.content
    if case.name == 'generation':
        checks['generation_constraints'] = all(word in (reply or '').lower() for word in ('fish', 'water'))
    if case.name in {'shell', 'shell_error', 'metta', 'query', 'episodes', 'restart'}:
        checks['tool_return_contains_expected_value'] = bool(case.expected and case.expected.lower() in joined.lower())
    if case.name == 'shell_timeout':
        checks['timeout_observed'] = 'timeout' in joined.lower()
    if case.name == 'nal':
        checks['native_conclusion'] = bool(re.search(r'-->\s+robin\s+animal', joined)) and 'stv' in joined
    if case.name == 'pln':
        checks['native_conclusion'] = bool(re.search(r'Inheritance\s+Pingu\s+Bird', joined)) and 'stv' in joined
    if case.name == 'websearch':
        checks['search_results_returned'] = 'TITLE:' in joined and 'SNIPPET:' in joined
    if case.name == 'policy':
        checks['tool_reported_denial'] = any(token in joined.lower() for token in ('denied', 'permission', 'write-failed'))
    if case.name == 'remember':
        checks['vector_store_grew'] = before_count is not None and chroma is not None and chroma['count'] is not None and chroma['count'] > before_count
        checks['document_stored'] = any(case.expected in item for item in (chroma or {}).get('documents', []))
    if case.name == 'restart':
        checks['vector_store_survived_recreation'] = chroma is not None and bool(chroma.get('count'))
    if case.name == 'reconnect':
        checks['resume_sequence_matches'] = bool((control or {}).get('resumed_sequence_matches'))
        checks['duplicate_not_executed'] = bool((control or {}).get('duplicate_not_executed'))
    if case.name == 'malformed':
        checks['connection_survived'] = bool((control or {}).get('connection_survived'))
    if case.name == 'restart':
        checks['fresh_container'] = bool((control or {}).get('fresh_container'))
    if case.name == 'git':
        checks['local_commit_created'] = (control or {}).get('commit_subject') == case.expected
    return {'status': 'passed' if all(checks.values()) else 'failed', 'checks': checks}


def unavailable_rows(provider):
    rows = []
    for name in PROVIDERS:
        if name != provider:
            rows.append({'id': 'provider_' + name, 'feature': 'Provider compatibility',
                'query': f'Generate a reply and invoke a tool using provider {name}.', 'response': None,
                'status': 'not_tested' if os.environ.get(PROVIDERS[name][0]) else 'unavailable',
                'reason': 'Run separately with --provider ' + name if os.environ.get(PROVIDERS[name][0]) else 'Provider key is not configured.'})
    for name in ('proof-create', 'proof-cycle', 'proof-status', 'proof-audit', 'proof-export',
                 'formal-search', 'formal-resume', 'statement-align', 'lemma-bridge'):
        rows.append({'id': 'atp_' + name, 'feature': 'ATP plugin',
            'query': f'Invoke the {name} proof action and report the authoritative receiver outcome.',
            'response': None, 'status': 'unavailable',
            'reason': 'ATP plugin is disabled for standalone Omega testing; no receiver is supplied.'})
    return rows


def save(output, report):
    for name, value in [('report.json', report), ('pairs.json', {row['query']: row['response'] for row in report['results']})]:
        temporary = output / (name + '.tmp')
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
        temporary.replace(output / name)


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=os.environ.get('OMEGA_PLAYGROUND_IMAGE', 'persistent-atp-omega:playground'))
    parser.add_argument('--build-image', action='store_true', help='Build the branch overlay from the pinned official base first')
    parser.add_argument('--base-image', default=os.environ.get('OMEGA_PLAYGROUND_BASE_IMAGE',
        'singularitynet/omega@sha256:a258d30a92b58b8b4bc104605ee57f20b2c2527b2a9ac3532186057098619f3b'))
    parser.add_argument('--omega-source', default=os.environ.get('OMEGA_PLAYGROUND_SOURCE', 'tmp/repos/Omega-ba185cd'))
    parser.add_argument('--provider', choices=tuple(PROVIDERS), default=os.environ.get('OMEGA_PLAYGROUND_PROVIDER', 'DeepSeek'))
    parser.add_argument('--model', default=os.environ.get('OMEGA_PLAYGROUND_MODEL'))
    parser.add_argument('--output', default=os.environ.get('OMEGA_PLAYGROUND_OUTPUT', '.runs/omega-capabilities'))
    parser.add_argument('--timeout', type=float, default=float(os.environ.get('OMEGA_PLAYGROUND_TIMEOUT', '120')))
    parser.add_argument('--startup-timeout', type=float, default=float(os.environ.get('OMEGA_PLAYGROUND_STARTUP_TIMEOUT', '300')))
    parser.add_argument('--cases', help='Comma-separated case IDs; default runs every standalone case')
    parser.add_argument('--list', action='store_true', help='List cases without starting Docker or calling a model')
    parser.add_argument('--keep-container', action='store_true')
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.startup_timeout <= 0:
        parser.error('timeouts must be positive')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]
    catalog = cases(run_id)
    if args.list:
        print(json.dumps([{'id': c.name, 'feature': c.feature, 'instruction': c.instruction} for c in catalog], indent=2))
        return 0
    selected = set(args.cases.split(',')) if args.cases else {c.name for c in catalog}
    if selected - {c.name for c in catalog}:
        parser.error('unknown cases: ' + ', '.join(sorted(selected - {c.name for c in catalog})))
    while True:
        expanded = selected | {dependency for name in selected for dependency in DEPENDENCIES.get(name, ())}
        if expanded == selected:
            break
        selected = expanded
    output = (Path(args.output) / run_id).resolve()
    output.mkdir(parents=True)
    model = provider_model(args.provider, args.model)
    report = {'run_id': run_id, 'started_at': utcnow(), 'provider': args.provider,
        'model': model, 'backend': 'live-provider', 'results': [], 'output': str(output)}
    runner = session = None
    def event(name, **fields):
        print(json.dumps({'event': name, **fields}), flush=True)
    try:
        ws_port = free_port()
        runner = Docker(args.image, args.provider, model, output, run_id, ws_port)
        if args.build_image:
            event('building_image', image=args.image)
            runner.build(args.omega_source, args.base_image)
        session = Session(ws_port, event)
        event('listening', url=f'ws://127.0.0.1:{ws_port}', output=str(output))
        runner.start()
        report.update(image=runner.image, container=runner.name, volume=runner.volume)
        session.wait(args.startup_timeout)
        pong = session.ping()
        report['results'].append({'id': 'connection', 'feature': 'WebSocket connectivity',
            'query': 'Connect Omega and answer a WebSocket ping.', 'response': pong,
            'response_source': 'websocket-control-frame', 'status': 'passed'})
        save(output, report)
        for case in catalog:
            tag = 'CASE_' + run_id + '_' + case.name
            instruction = case.instruction
            history = runner.read(MEMORY + '/history.metta') or ''
            timestamps = re.findall(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', history)
            instruction = instruction.replace('{episode_time}', timestamps[-1] if timestamps else datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
            query_text = instruction + f' At completion, use send once with a reply beginning {tag}. Do not send a preliminary acknowledgement. Stop this task after reporting.'
            row = {'id': case.name, 'feature': case.feature, 'query': query_text,
                'response': None, 'response_source': 'omega-agent', 'started_at': utcnow()}
            if case.name not in selected:
                row.update(status='not_tested', reason='Not selected by --cases.')
                report['results'].append(row)
                continue
            event('case_started', id=case.name, feature=case.feature)
            before = runner.chroma() if case.name == 'remember' else None
            control = None
            try:
                if case.action == 'reconnect':
                    control = session.reconnect(args.timeout)
                elif case.action == 'malformed':
                    control = {'connection_before': session.connections}
                    session.malformed()
                elif case.action == 'restart':
                    (output / 'before-recreation.log').write_text(runner.logs())
                    previous = session.connections
                    old_container = runner.container_id
                    runner.remove()
                    runner.start()
                    session.wait(args.startup_timeout, after=previous)
                    control = {'fresh_container': runner.container_id != old_container,
                        'previous_container': old_container, 'container': runner.container_id}
                    history = runner.read(MEMORY + '/history.metta') or ''
                since = utcnow()
                row['response'] = session.ask(query_text, tag, args.timeout)
                # send may arrive just before the loop appends its history.
                for _ in range(30):
                    current = runner.read(MEMORY + '/history.metta') or ''
                    delta = current[len(history):] if current.startswith(history) else current
                    if tag in delta:
                        break
                    time.sleep(.1)
                logs = runner.logs(since)
                records = command_records(logs)
                tools, returns = sorted({item['tool'] for item in records}), returned_values(logs)
                if case.action == 'reconnect':
                    control['duplicate_not_executed'] = 'DUPLICATE_FRAME_MUST_NOT_EXECUTE' not in delta
                if case.action == 'malformed':
                    control['connection_survived'] = control['connection_before'] == session.connections
                if case.action == 'git':
                    control = {'commit_subject': runner.exec('git', '-C', str(Path(case.file).parent), 'log', '-1', '--format=%s').stdout.strip()}
                actual_file = runner.read(case.file) if case.file else None
                chroma = runner.chroma() if case.name in {'remember', 'restart'} else None
                row.update(evaluate(case, row['response'], tools, returns, actual_file=actual_file,
                    before_count=(before or {}).get('count'), chroma=chroma, control=control, records=records))
                row.update(tools=tools, tool_returns=returns, command_records=records, control=control)
                if case.file:
                    row['file_observation'] = {'path': case.file, 'content': actual_file}
                if chroma is not None:
                    row['chroma_observation'] = chroma
                (output / (case.name + '.history')).write_text(delta)
                (output / (case.name + '.log')).write_text(logs)
            except Exception as error:
                row.update(status='failed', reason=runner.scrub(str(error)))
                (output / (case.name + '.log')).write_text(runner.logs())
            row['finished_at'] = utcnow()
            report['results'].append(row)
            save(output, report)
            event('case_finished', id=case.name, status=row['status'])
        report['results'].extend(unavailable_rows(args.provider))
    except Exception as error:
        report['setup_error'] = runner.scrub(str(error)) if runner else str(error)
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
        recorded = {row['id'] for row in report['results']}
        for case in catalog:
            if case.name not in recorded:
                report['results'].append({'id': case.name, 'feature': case.feature,
                    'query': case.instruction, 'response': None, 'status': 'not_tested',
                    'reason': 'Run did not reach this case; inspect setup_error.'})
        for row in unavailable_rows(args.provider):
            if row['id'] not in {item['id'] for item in report['results']}:
                report['results'].append(row)
        report['finished_at'] = utcnow()
        report['summary'] = {status: sum(row['status'] == status for row in report['results'])
            for status in ('passed', 'failed', 'unavailable', 'not_tested')}
        save(output, report)
        event('finished', output=str(output), **report['summary'])
    return 1 if report.get('setup_error') or report['summary']['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
