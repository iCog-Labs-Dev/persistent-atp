"""Local model/channel fixture for the full Omega image test; never paid inference."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from omegaclaw.api import serve
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService


def main():
    root, omega, address, container = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    sys.path[:0] = [str(omega / 'src'), str(omega)]
    from Autotests.mock.comm import CommMockServer
    communication = CommMockServer((address, 9766))
    token = 'reference-container-test-' + os.urandom(16).hex()
    provider_token = 'provider-container-test-' + os.urandom(16).hex()
    service = OperationService(root / 'receiver')
    receiver = ReferenceReceiver(service)
    api = serve(service, token, receiver=receiver)
    lock = threading.Lock()
    seen = set()
    calls = []
    spec = json.dumps({'theorem': '∀ n : Nat, n = n', 'definitions': {}, 'assumptions': [],
        'quantifier_domains': {'n': 'Nat'}, 'allowed_background': [], 'targets': ['literal theorem'],
        'verification_policy': 'independent Lean replay'}, ensure_ascii=False)

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == '/chat/completions'
            assert self.headers.get('Authorization') == 'Bearer ' + provider_token
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['tool_choice'] == 'required'
            user = next((m['content'] for m in reversed(body['messages']) if m['role'] == 'user'), '')
            name, arguments = 'nop', {}
            with lock:
                for prompt, action, args in [
                    ('CONTAINER_CREATE', 'proof-create', {'spec': spec, 'workspace': 'reference'}),
                    *[(f'CONTAINER_STATUS_{i}', 'proof-status', {'proof': proof, 'workspace': 'reference'})
                      for i in (1, 2) for proof in service.store.proof_ids()]]:
                    if prompt in user and prompt not in seen:
                        seen.add(prompt)
                        name, arguments = action, args
                        calls.append(action)
                        break
            payload = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                'content': None, 'tool_calls': [{'id': 'test-' + os.urandom(8).hex(), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}}]}
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    for server in (api, model):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    (root / 'bridge.env').write_text(f'PERSISTENT_ATP_OMEGA_TOKEN={token}\n'
        f'PERSISTENT_ATP_OMEGA_URL=http://127.0.0.1:{api.server_port}\n'
        'PERSISTENT_ATP_OMEGA_WORKSPACE=reference\nOMEGA_persistentAtpEnabled=enabled\n'
        'OMEGA_persistentAtpWorkspace=reference\n' + f'DEEPSEEK_API_KEY={provider_token}\n')
    (root / 'bridge.env').chmod(0o600)
    (root / 'ready.json').write_text(json.dumps({'model_port': model.server_port}))
    running = True

    def stop(*args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    started = time.monotonic()
    phase = 0
    status_number = 0
    previous_status_ids = set()
    try:
        while running and time.monotonic() - started < 300:
            message = communication.getLastMessage()
            if message and phase == 0:
                communication.send_message('CONTAINER_CREATE', timeout=3)
                phase = 1
            if phase == 1 and service.store.proof_ids():
                proof = service.store.proof_ids()[0]
                assert service.store.verify_chain(proof) == 1
                communication.send_message('CONTAINER_STATUS_1', timeout=3)
                status_number, phase = 1, 2
            if phase == 3 and message:
                communication.send_message('CONTAINER_STATUS_2', timeout=3)
                status_number, phase = 2, 2
            if phase == 2:
                result = subprocess.run(['docker', 'exec', '--user', '65534:65534', container, 'cat',
                    '/PeTTa/repos/Omega/memory/persistent_atp/workflow.json'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    cache = json.loads(result.stdout)
                    reads = {row['id'] for row in cache['intents'].values()
                             if row['kind'] == 'proof-status' and row['state'] == 'completed'}
                    history = subprocess.run(['docker', 'exec', '--user', '65534:65534', container, 'cat',
                        '/PeTTa/repos/Omega/memory/history.metta'], capture_output=True, text=True, timeout=5)
                    if any(identity in history.stdout for identity in reads - previous_status_ids):
                        assert token not in history.stdout and provider_token not in history.stdout
                        assert token not in result.stdout and provider_token not in result.stdout
                        assert len(service.store.proof_ids()) == 1
                        assert service._status(proof)['formal_verified'] is False
                        assert calls.count('proof-create') == 1
                        (root / 'result.json').write_text(json.dumps({'stage': status_number,
                            'revision': 1, 'formal_verified': False, 'gate_chain_verified': True,
                            'receiver_mode': 'reference-scripted', 'proxy_forwarded': True}))
                        previous_status_ids = reads
                        phase = 3 if status_number == 1 else 4
            time.sleep(0.25)
    finally:
        communication.stop(3)
        for server in (api, model):
            server.shutdown()
            server.server_close()
        receiver.close()


if __name__ == '__main__':
    main()
