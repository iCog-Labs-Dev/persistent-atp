"""Readiness is a bounded background hint, never proof authority."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from omegaclaw.api import serve
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
from persistent_atp_client import ActionRequest, Budget, FormalSearch
from persistent_atp_client.errors import TransientUnavailable


class Probe:
    def __init__(self):
        self.config = SimpleNamespace(mode='real')
        self.entered, self.release = threading.Event(), threading.Event()
        self.result, self.calls = True, 0

    def ready(self):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return self.result


def wait_checked(receiver):
    end = time.monotonic() + 2
    while time.monotonic() < end:
        with receiver._lock:
            if receiver._readiness_pending is None:
                return
        time.sleep(.005)
    pytest.fail('readiness worker did not finish')


def test_slow_readiness_keeps_capabilities_and_http_health_responsive(tmp_path):
    service = OperationService(tmp_path)
    probe = Probe()
    service.formal = probe
    receiver = ReferenceReceiver(service)
    server = serve(service, 'readiness-regression-test-token', receiver=receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(httpx.get, url + '/v2/capabilities',
                headers={'Authorization': 'Bearer readiness-regression-test-token'}, timeout=2)
            assert probe.entered.wait(1)
            response = pending.result(timeout=1)
            assert response.status_code == 200 and response.json()['backend_mode'] == 'unavailable'
            assert httpx.get(url + '/health', timeout=.5).status_code == 200
            assert probe.calls == 1 and not probe.release.is_set()
            # Unknown readiness defers mathematical admission, without probing
            # again or committing an operation while the provider is blocked.
            request = ActionRequest('pending-math', 'reference', FormalSearch('p1', 'p1/c-1', Budget(1, 100)))
            with pytest.raises(TransientUnavailable):
                asyncio.run(receiver.submit(request))
            assert receiver._db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0
    finally:
        probe.release.set()
        server.shutdown()
        server.server_close()
        receiver.close()
        service.close()


def test_readiness_expiry_is_conservative_and_probe_is_deduplicated(tmp_path):
    service = OperationService(tmp_path)
    probe = Probe()
    service.formal = probe
    receiver = ReferenceReceiver(service)
    clock = [time.monotonic()]
    receiver._readiness_clock = lambda: clock[0]
    try:
        assert probe.entered.wait(1)
        for _ in range(10):
            assert asyncio.run(receiver.capabilities()).backend_mode == 'unavailable'
        assert probe.calls == 1
        probe.release.set()
        wait_checked(receiver)
        assert asyncio.run(receiver.capabilities()).backend_mode == 'real'
        probe.entered.clear()
        probe.release.clear()
        probe.result = False
        clock[0] += 6
        assert asyncio.run(receiver.capabilities()).backend_mode == 'unavailable'
        assert probe.entered.wait(1)
        assert probe.calls == 2
        probe.release.set()
        wait_checked(receiver)
        unavailable = asyncio.run(receiver.capabilities())
        assert unavailable.backend_mode == 'unavailable' and 'formal-search' not in unavailable.actions
        assert 'proof-audit' in unavailable.actions and 'reconcile-search' in unavailable.controls
    finally:
        probe.release.set()
        receiver.close()
        service.close()


def test_replaced_provider_cannot_publish_old_readiness(tmp_path):
    service = OperationService(tmp_path)
    old, new = Probe(), Probe()
    service.formal = old
    receiver = ReferenceReceiver(service)
    try:
        assert old.entered.wait(1)
        service.formal = new
        assert asyncio.run(receiver.capabilities()).backend_mode == 'unavailable'
        old.release.set()
        assert new.entered.wait(1)
        assert asyncio.run(receiver.capabilities()).backend_mode == 'unavailable'
        new.result = False
        new.release.set()
        wait_checked(receiver)
        assert asyncio.run(receiver.capabilities()).backend_mode == 'unavailable'
    finally:
        old.release.set()
        new.release.set()
        receiver.close()
        service.close()


def test_restart_does_not_reuse_cached_provider_availability(tmp_path):
    service = OperationService(tmp_path)
    first, second = Probe(), Probe()
    first.release.set()
    service.formal = first
    receiver = ReferenceReceiver(service)
    reopened = None
    try:
        wait_checked(receiver)
        assert asyncio.run(receiver.capabilities()).backend_mode == 'real'
        receiver.close()
        service.formal = second
        reopened = ReferenceReceiver(service)
        assert second.entered.wait(1)
        pending = asyncio.run(reopened.capabilities())
        assert pending.backend_mode == 'unavailable' and not pending.independent_replay
        second.result = False
        second.release.set()
        wait_checked(reopened)
        assert asyncio.run(reopened.capabilities()).backend_mode == 'unavailable'
    finally:
        first.release.set()
        second.release.set()
        receiver.close()
        if reopened is not None:
            reopened.close()
        service.close()
