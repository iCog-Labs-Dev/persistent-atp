"""Real socket deadlines, including streams that never exceed idle timeout."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
from socketserver import BaseRequestHandler, ThreadingTCPServer
import threading
import time

import pytest

from mathproof.maths_ai_http import MathsAIAPIError, MathsAIHTTPFormalATP


@pytest.fixture
def slow_provider():
    entered, release = threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(self.path)
            entered.set()
            try:
                if self.path == '/headers':
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    for _ in range(20):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        if release.wait(.04): return
                    self.wfile.write(b'\r\nContent-Length: 2\r\n\r\n{}')
                    return
                body = b'{}' + b' ' * 18
                self.send_response(503 if self.path == '/error' else 200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if release.wait(.04): return
            except (OSError, ConnectionError):
                pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            requests.append(self.path)
            entered.set()
            release.wait(5)  # Remote work may outlive the local HTTP connection.
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', requests, entered, release
    release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def network_processes(monkeypatch):
    processes = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        if any(str(part).endswith('_bounded_http.py') for part in args[0]):
            processes.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    yield processes
    for child in processes:
        assert child.poll() is not None, 'network helper survived its request deadline'
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)


@pytest.mark.parametrize('path,binary', [('/body', False), ('/headers', False), ('/error', False), ('/artifact', True)])
def test_total_deadline_terminates_slow_stream(slow_provider, network_processes, path, binary):
    url, requests, _, _ = slow_provider
    client = MathsAIHTTPFormalATP(url, artifacts=None, timeout=.25)
    start = time.monotonic()
    with pytest.raises(MathsAIAPIError, match='deadline|timed out'):
        client._transport('GET', path, binary=binary)
    assert time.monotonic() - start < 1
    assert requests == [path]
    assert len(network_processes) == 1


def test_total_deadline_terminates_blocked_dns(tmp_path, monkeypatch, network_processes):
    marker = tmp_path / 'dns-entered'
    (tmp_path / 'sitecustomize.py').write_text(
        'import socket, time\n'
        'def blocked(*args, **kwargs):\n'
        f'    open({str(marker)!r}, "w").close()\n'
        '    while True: time.sleep(.05)\n'
        'socket.getaddrinfo = blocked\n')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    client = MathsAIHTTPFormalATP('http://deadline.invalid', artifacts=None, timeout=.4)
    start = time.monotonic()
    with pytest.raises(MathsAIAPIError, match='deadline|timed out'):
        client._transport('GET', '/never-resolves')
    assert time.monotonic() - start < 1
    assert marker.exists() and len(network_processes) == 1


def test_concurrent_expired_posts_are_not_retried(slow_provider, network_processes):
    url, requests, entered, release = slow_provider
    def send(index):
        client = MathsAIHTTPFormalATP(url, artifacts=None, timeout=.4)
        with pytest.raises(MathsAIAPIError):
            client._transport('POST', '/run-' + str(index), {'original': index})
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send, range(2)))
    assert entered.is_set() and not release.is_set()
    assert sorted(requests) == ['/run-0', '/run-1']
    assert len(network_processes) == 2


def test_total_deadline_terminates_stalled_tls_handshake(network_processes):
    entered, release = threading.Event(), threading.Event()
    class SilentPeer(BaseRequestHandler):
        def handle(self):
            entered.set()
            release.wait(3)
    server = ThreadingTCPServer(('127.0.0.1', 0), SilentPeer)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = MathsAIHTTPFormalATP(f'https://127.0.0.1:{server.server_address[1]}', artifacts=None, timeout=.4)
        with pytest.raises(MathsAIAPIError, match='deadline|timed out'):
            client._transport('GET', '/handshake')
        assert entered.is_set() and len(network_processes) == 1
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
