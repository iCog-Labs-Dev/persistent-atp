"""Integrity of the runtime schemas and recorded provider test fixtures."""
import json
from pathlib import Path

from mathproof.formal_v2 import CONTRACT_DIR, address, validate, verify_package


def test_pinned_schemas_and_recorded_provider_examples():
    verify_package()
    manifest = json.loads((CONTRACT_DIR / 'manifest.json').read_bytes())
    fixtures = Path(__file__).resolve().parents[3] / 'fixtures/formal_v2'
    expected = {name for name in manifest['files_sha256'] if name.startswith('examples/')}
    actual = {f'examples/{path.name}' for path in (fixtures / 'examples').glob('*.json')}
    assert actual == expected
    for name in sorted(expected):
        raw = (fixtures / name).read_bytes()
        assert address(raw) == manifest['files_sha256'][name]
        if name == 'examples/index.json':
            continue
        response = json.loads(raw)['response']
        if 'schema' in response:
            assert validate(response['schema'].removesuffix('.v2'), response['body']) == [], name
