"""Acceptance reports must distinguish model claims from observed effects."""
import json
import threading

import pytest

from omegaclaw.playground import Case, Session, evaluate, returned_values, save, unavailable_rows, command_records, cases, provider_model, proxy_template


def test_model_claim_does_not_substitute_for_tool_or_file_evidence():
    case = Case('file_write', 'files', 'write a file', ('write-file',), file='/test', content='expected')
    result = evaluate(case, 'I wrote the expected file', [], [], actual_file=None)
    assert result['status'] == 'failed'
    assert not result['checks']['tool:write-file']
    assert not result['checks']['filesystem_effect']
    assert evaluate(case, 'done', ['write-file'], [], actual_file='expected')['status'] == 'passed'


def test_native_computation_requires_actual_return_value():
    case = Case('metta', 'reasoning', 'evaluate', ('metta',), '40')
    assert evaluate(case, '40', ['metta'], ['empty result'])['status'] == 'failed'
    assert evaluate(case, '40', ['metta'], ['[40]'])['status'] == 'passed'


def test_tool_results_are_not_inferred_from_request_or_compiled_source():
    log = '\n'.join([
        'REQUEST: please compute 391',
        'compiled source: (COMMAND_RETURN: ((shell "echo 391") "SUCCESS, RETURN: 391"))',
        '2026-11-19 03:00:00 | INFO | loop | (COMMAND_RETURN: ((shell "python") "SUCCESS, RETURN: [391]"))',
    ])
    assert returned_values(log) == ['[391]']


def test_quoted_tool_claim_in_send_does_not_count_as_execution():
    log = '2026-11-19 03:00:00 | INFO | loop | (COMMAND_RETURN: ((send "I used (shell fake)") "SUCCESS, RETURN: [None]"))'
    assert [record['tool'] for record in command_records(log)] == ['send']


def test_other_tools_cannot_supply_missing_native_computation_result():
    case = Case('metta', 'reasoning', 'evaluate', ('metta',), '40')
    result = evaluate(case, '40', ['metta', 'shell'], ['40'],
        records=[{'tool': 'metta', 'value': None}, {'tool': 'shell', 'value': '40'}])
    assert result['status'] == 'failed'


def test_append_case_checks_line_semantics():
    case = next(item for item in cases('TEST') if item.name == 'file_append')
    assert case.content == 'omega-TEST\n-appended\n'
    assert evaluate(case, '-appended', ['append-file', 'read-file'], [], actual_file='omega-TEST-appended')['status'] == 'failed'


def test_pair_export_keeps_actual_response_and_null_for_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv('ASIONE_API_KEY', raising=False)
    missing = unavailable_rows('DeepSeek')
    assert all(row['response'] is None for row in missing)
    report = {'results': [{'query': 'What is fish?', 'response': 'A fish is an aquatic animal.'}, *missing]}
    save(tmp_path, report)
    pairs = json.loads((tmp_path / 'pairs.json').read_text())
    assert pairs['What is fish?'] == 'A fish is an aquatic animal.'
    assert pairs[missing[0]['query']] is None
    assert not any(row['id'].startswith('channel_') for row in report['results'] if 'id' in row)


def test_asi_model_setting_and_explicit_override(monkeypatch):
    monkeypatch.setenv('ASIONE_MODEL', 'asi-test-model')
    assert provider_model('ASIOne') == 'asi-test-model'
    assert provider_model('ASIOne', 'explicit-model') == 'explicit-model'


def test_asi_endpoint_is_forwarded_by_trusted_proxy_only():
    template = '''listen 8080;
location /asione/ {
 proxy_pass https://api.asi1.ai/v1/;
 proxy_set_header Host api.asi1.ai;
 proxy_set_header Authorization "Bearer ${ASIONE_API_KEY}";
}
location /deepseek/ { proxy_pass https://api.deepseek.com/; }
'''
    actual = proxy_template(template, 9001, 'ASIOne', 'https://llm.a.singularitycompute.com/v1')
    assert 'proxy_pass https://llm.a.singularitycompute.com/v1/;' in actual
    assert 'Host llm.a.singularitycompute.com;' in actual
    assert '${ASIONE_API_KEY}' in actual
    assert 'https://api.deepseek.com/' in actual
    assert proxy_template(template, 9001, 'DeepSeek', 'https://unused.example') == template.replace('listen 8080;', 'listen 127.0.0.1:9001;')


@pytest.mark.parametrize('endpoint', ['https://user:secret@example.com/v1',
    'https://example.com/v1;include /tmp/file;', 'https://example.com/v1?key=secret',
    'http://external.example/v1'])
def test_asi_endpoint_rejects_embedded_secrets_and_invalid_proxy_values(endpoint):
    with pytest.raises(ValueError):
        proxy_template('', 9001, 'ASIOne', endpoint)


def test_session_correlates_replies_and_acknowledges_duplicate_delivery():
    pytest.importorskip('websockets')
    from websockets.sync.client import connect
    session = Session(0, lambda *args, **kwargs: None)
    port = session.server.socket.getsockname()[1]
    failures = []
    def peer():
        try:
            with connect(f'ws://127.0.0.1:{port}') as ws:
                ws.send(json.dumps({'type': 'resume', 'last_seen_seq': None}))
                request = json.loads(ws.recv(timeout=5))
                assert request['text'] == 'real request'
                for text in ('CASE_TEST actual response', 'CASE_TEST duplicate response'):
                    ws.send(json.dumps({'type': 'agent_message', 'client_seq': 'same', 'text': text}))
                    assert json.loads(ws.recv(timeout=5))['type'] == 'ack'
        except Exception as error:
            failures.append(error)
    thread = threading.Thread(target=peer)
    thread.start()
    try:
        assert session.ask('real request', 'CASE_TEST', 5) == 'CASE_TEST actual response'
        thread.join(timeout=5)
        assert not thread.is_alive() and not failures
        assert len(session.frames) == 1
    finally:
        session.close()
