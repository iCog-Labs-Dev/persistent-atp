"""Build and boot the verified official-image overlay through its real proxy."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest

from omegaclaw.installer import install, bundle_runtime
from omegaclaw.runtime import DEFAULT_OMEGA_ROOT


def docker(*args, timeout=30, **kwargs):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout, **kwargs)


def wait_for(path, condition, process, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, 'local model/receiver fixture exited; inspect fixture.log'
        if path.exists():
            value = json.loads(path.read_text())
            if condition(value):
                return value
        time.sleep(0.25)
    pytest.fail(f'container validation timed out waiting for {path.name}')


def test_full_official_container_proxy_tools_history_and_restart(tmp_path):
    base = os.environ.get('OMEGA_CONTAINER_BASE_IMAGE')
    if not base:
        if os.environ.get('OMEGA_CONTAINER_REQUIRED') == '1':
            pytest.fail('OMEGA_CONTAINER_BASE_IMAGE must name a digest-pinned official Omega image')
        pytest.skip('set OMEGA_CONTAINER_BASE_IMAGE for the full image integration test')
    assert re.fullmatch(r'singularitynet/omega@sha256:[0-9a-f]{64}', base)
    assert docker('image', 'inspect', base).returncode == 0, 'pull the official image before this test'
    omega = tmp_path / 'Omega'
    shutil.copytree(Path(os.environ.get('OMEGA_PINNED_ROOT', str(DEFAULT_OMEGA_ROOT))), omega,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    install(omega)
    context = tmp_path / 'context'
    bundle_runtime(omega, context, base)
    identity = uuid.uuid4().hex
    image, container, volume = 'omega-atp-test:' + identity, 'omega-atp-test-' + identity, 'omega-atp-memory-' + identity
    fixture = None
    log = None
    try:
        build = docker('build', '-t', image, str(context), timeout=180)
        assert build.returncode == 0, build.stderr[-4000:]
        deployed = docker('run', '--rm', '--network=none', '--entrypoint=cat', image,
                          '/opt/nginx/nginx.conf.template')
        assert deployed.returncode == 0 and 'location = /deepseek/chat/completions {' in deployed.stdout
        source_root = Path(__file__).resolve().parents[2]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(source_root / 'src'),
            str(source_root / 'packages/persistent-atp-client/src')])}
        address = '127.0.' + str(10 + int(identity[:2], 16) % 240) + '.2'
        log = (tmp_path / 'fixture.log').open('w')
        fixture = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('container_fixture.py')),
            str(tmp_path), str(omega), address, container], env=env, stdout=log, stderr=log)
        ready = wait_for(tmp_path / 'ready.json', lambda _: True, fixture, timeout=20)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            gateway_port = reservation.getsockname()[1]
        # Exercise the actual deployed template with only test socket/upstream
        # substitutions. Unused upstream hosts resolve locally; no paid calls.
        template = deployed.stdout.replace('listen 8080;', f'listen 127.0.0.1:{gateway_port};')
        template = template.replace('https://api.deepseek.com/chat/completions;',
                                    f'http://127.0.0.1:{ready["model_port"]}/chat/completions;')
        template_file = tmp_path / 'nginx.conf.template'
        template_file.write_text(template)
        template_file.chmod(0o644)
        hosts = sorted(set(re.findall(r'https://([^/;]+)', template)))
        command = ['run', '-d', '-it', '--name', container, '--network=host', '--init',
            '--security-opt=no-new-privileges:true', '--tmpfs', '/tmp:size=64m,mode=1777',
            '--tmpfs', '/var/tmp:size=64m,mode=1777', '--tmpfs', '/run:size=16m,mode=755',
            '--volume', volume + ':/PeTTa/repos/Omega/memory', '--env-file', str(tmp_path / 'bridge.env'),
            '--mount', f'type=bind,src={template_file},dst=/opt/nginx/test-template,readonly',
            '-e', 'NGINX_TEMPLATE=/opt/nginx/test-template', '-e', 'OMEGA_AUTH_SECRET=',
            '-e', 'IMPORT_KB_ON_START=0', '-e', 'TEST_SERVER_IP=' + address]
        for host in hosts:
            command.extend(['--add-host', host + ':127.0.0.1'])
        command.extend([image, 'commchannel=test', 'provider=DeepSeek', 'embeddingprovider=Local',
            'model=deepseek-flash', 'TEST_SERVER_IP=' + address,
            f'GATEWAY_URL=http://localhost:{gateway_port}',
            'securityPolicyPath=/PeTTa/repos/Omega/profile/policy.yaml'])
        started = docker(*command)
        assert started.returncode == 0, started.stderr
        first = wait_for(tmp_path / 'result.json', lambda row: row['stage'] == 1, fixture)
        assert first['proxy_forwarded'] and first['gate_chain_verified'] and not first['formal_verified']
        restart = docker('restart', container)
        assert restart.returncode == 0, restart.stderr
        second = wait_for(tmp_path / 'result.json', lambda row: row['stage'] == 2, fixture)
        assert second['receiver_mode'] == 'reference-scripted' and second['revision'] == 1
    finally:
        if docker('inspect', container).returncode == 0:
            logs = docker('logs', container)
            (tmp_path / 'container.log').write_text(logs.stdout + logs.stderr)
        docker('rm', '-f', container)
        if fixture:
            fixture.terminate()
            try:
                fixture.wait(timeout=15)
            except subprocess.TimeoutExpired:
                fixture.kill()
                fixture.wait(timeout=5)
        if log:
            log.close()
        docker('volume', 'rm', volume)
        docker('image', 'rm', image)
