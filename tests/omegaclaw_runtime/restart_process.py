"""Fresh Janus interpreter; model I/O is forbidden throughout managed recovery."""
import json
import os
from pathlib import Path
import sys
import time

import janus_swi
from petta import PeTTa

petta_root, omega_root = os.environ['PETTA_TEST_ROOT'], os.environ['OMEGA_TEST_ROOT']
phase, boundary = sys.argv[1], os.environ['OMEGA_RESTART_BOUNDARY']
runtime = PeTTa(petta_path=petta_root)


def execute(expression):
    values = runtime.process_metta_string(expression)
    assert values, expression
    return values


janus_swi.query_once('asserta(working_dir(Dir))', {'Dir': petta_root})
execute('!(import! &self (library lib_import))')
for expression in (Path(omega_root) / 'run.metta').read_text().splitlines()[1:3]:
    execute(expression)
for module in ('utils.py', 'helper.py', 'utils', 'logger.py', 'log', 'config.py', 'config',
               'skills', 'plugin.py', 'plugin'):
    execute(f'!(import! &self (library Omega ./src/{module}))')
sys.path[:0] = [omega_root + '/src', omega_root]
import config
config.init_config([])
history = Path(omega_root) / 'memory/history.metta'
history.parent.mkdir(exist_ok=True)
if not history.exists():
    history.write_text('')
os.environ['OPENAI_API_KEY'] = 'mock-no-openai-network'
execute('!(import! &self (library lib_llm))')
for module in ('channels.py', 'channels', 'providers', 'memory', 'loop'):
    execute(f'!(import! &self (library Omega ./src/{module}))')
execute(f'!(loadMettaPlugin persistent_atp {json.dumps(omega_root + "/plugins/persistent_atp")})')
import plugin
plugin.loadPythonPlugin('wschat', omega_root + '/channels')
import wschat
import channels
import providers
import omega_persistent_atp_bridge as bridge
import persistent_atp_channel as hook

adapter = bridge._get_adapter()
if phase == 'restore' and boundary == 'delivery':
    # Simulate the elapsed delivery backoff without changing persisted task or
    # SDK deadlines, and without sleeping for the full 30-second retry window.
    adapter._clock = lambda: time.time() + 31
wschat._ws_url = 'ws://managed-restart-fixture/session'
wschat._connected = True
messages = []


def send(payload, **kwargs):
    messages.append(payload)
    if not (phase == 'seed' and boundary == 'delivery'):
        wschat._handle_frame(json.dumps({'type': 'ack', 'client_seq': payload['client_seq']}))


wschat._send_json = send
if phase == 'seed':
    spec = {'theorem': 'True', 'definitions': {}, 'assumptions': [], 'quantifier_domains': {},
        'allowed_background': [], 'targets': ['True'], 'verification_policy': {'independent_lean_replay': True}}
    frame = {'type': 'user_message', 'seq': 1, 'text': 'Managed restart fixture',
        'persistent_atp_task': {'spec': spec, 'max_cycles': 1, 'max_research': 0, 'timeout_seconds': 240}}
    wschat._handle_frame(json.dumps(frame))
    if boundary == 'active':
        tick = adapter.tasks.tick
        def pause_after_admission(identity):
            # Stop the first process at an actual SDK admission boundary.
            if any(row.get('ref') for row in adapter.state.snapshot()['intents'].values()):
                return
            tick(identity)
        adapter.tasks.tick = pause_after_admission
else:
    assert len(adapter.state.snapshot()['tasks']) == 1
assert hook.model_suspended()
model_calls = 0


def forbidden_model(*args, **kwargs):
    global model_calls
    model_calls += 1
    raise AssertionError('managed recovery attempted a model call')


providers.llmProviderChat = forbidden_model


class ManagedChannel(channels.CommChannel):
    received = 0
    def receive(self):
        self.received += 1
        task = next(iter(adapter.state.snapshot()['tasks'].values()))
        ready = task['phase'] == 'done'
        if phase == 'seed' and boundary == 'active':
            ready = any(row.get('ref') for row in adapter.state.snapshot()['intents'].values())
        elif phase == 'seed' and boundary == 'delivery':
            ready = bool(messages) and task['phase'] == 'delivery'
        if ready and self.received >= 3:
            raise RuntimeError('MANAGED_RESTART_COMPLETE')
        if self.received > 1500:
            raise RuntimeError('MANAGED_RESTART_TIMEOUT')
        return ''
    def send(self, message):
        raise AssertionError('managed task bypassed its durable outbox')


channels.registerCommChannel('managed-restart', ManagedChannel())
channels.commChannelStart('managed-restart')
config.init_config(['sleepInterval=0.01', 'maxNewInputLoops=0', 'maxWakeLoops=0', 'wakeupInterval=3600'])
execute('!(configure maxHistory 200000)')
assert str(janus_swi.query_once("(nb_current('&nextWakeAt', _) -> Exists = true ; Exists = false)")['Exists']) == 'false'
execute('!(initLoop)')
try:
    execute('!(omega 2)')
except janus_swi.PrologError as exc:
    assert 'MANAGED_RESTART_COMPLETE' in str(exc), str(exc)
else:
    raise AssertionError('managed loop did not reach its bounded stop')
wschat._connected = False
bridge._pool.shutdown(wait=True)
state = adapter.state.snapshot()
task = next(iter(state['tasks'].values()))
requests = {key: {'request':row.get('request'), 'ref':row.get('ref'), 'deadline':row.get('deadline')}
            for key, row in state['intents'].items()}
assert model_calls == 0
assert janus_swi.query_once("nb_getval('&nextWakeAt', Wake)")['Wake'] > time.time()
print('RESTART_RESULT=' + json.dumps({'pid':os.getpid(), 'task':task, 'requests':requests,
                                    'model_calls':model_calls}, sort_keys=True), flush=True)
adapter.close()
