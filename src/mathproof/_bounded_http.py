"""One disposable HTTP exchange; no artifacts, journal, leases or retries.

Run this file directly in a fresh interpreter so DNS and socket operations can
be terminated, including calls that cannot be cancelled in a Python thread.
Credentials and request bytes travel through stdin, never command arguments.
"""
import base64
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MAX_BODY = 32 * 1024 * 1024
MAX_INPUT = 16 * 1024 * 1024


def request(method, url, headers, body, *, deadline):
    """Return (status, bytes, content hash), after reaping the HTTP process."""
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise TimeoutError('formal transport deadline elapsed')
    payload = json.dumps({'method': method, 'url': url, 'headers': headers,
        'body': None if body is None else base64.b64encode(body).decode('ascii'),
        'timeout': deadline - time.monotonic()}, allow_nan=False).encode()
    if len(payload) > MAX_INPUT:
        raise ValueError('formal request exceeds the transport input bound')
    if time.monotonic() >= deadline:
        raise TimeoutError('formal transport deadline elapsed')
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('formal transport deadline elapsed')
        try:
            output, _ = child.communicate(payload, timeout=left)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError('formal transport deadline elapsed') from exc
        if time.monotonic() >= deadline:
            raise TimeoutError('formal transport deadline elapsed')
        if child.returncode or len(output) > MAX_BODY * 4 // 3 + 4096:
            raise OSError('formal HTTP helper did not return a bounded response')
        reply = json.loads(output)
        if 'error' in reply:
            raise OSError(reply['error'])
        if (set(reply) != {'status', 'body', 'hash'} or type(reply['status']) is not int
                or not 100 <= reply['status'] <= 599 or not isinstance(reply['body'], str)
                or reply['hash'] is not None and not isinstance(reply['hash'], str)):
            raise OSError('formal HTTP helper returned an invalid response')
        raw = base64.b64decode(reply['body'], validate=True)
        if len(raw) > MAX_BODY:
            raise OSError('formal response exceeds the transport bound')
        return reply['status'], raw, reply['hash']
    finally:
        if child.poll() is None:
            child.kill()
        # communicate closes the pipes and reaps the child, also after timeout
        # or caller cancellation. There is no surviving network worker.
        child.communicate()


def _exchange(raw):
    value = json.loads(raw)
    body = None if value['body'] is None else base64.b64decode(value['body'], validate=True)
    req = Request(value['url'], data=body, headers=value['headers'], method=value['method'])
    try:
        response = urlopen(req, timeout=value['timeout'])
    except HTTPError as exc:
        response = exc
    with response:
        # Error diagnostics have a smaller bound; complete proof/artifact
        # responses are never truncated and accepted as complete evidence.
        limit = 65536 if response.status >= 400 else MAX_BODY
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('provider response exceeds the transport byte bound')
        return {'status': response.status, 'hash': response.headers.get('X-Content-Hash'),
            'body': base64.b64encode(data).decode('ascii')}


def _main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError('formal request exceeds the transport input bound')
        reply = _exchange(raw)
    except Exception as exc:
        reply = {'error': str(exc)[:500]}
    sys.stdout.write(json.dumps(reply))


if __name__ == '__main__':
    _main()
