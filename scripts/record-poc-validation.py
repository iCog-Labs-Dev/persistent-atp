#!/usr/bin/env python3
"""Record completed PoC checks using the same Python interpreter as pytest.

Run after full/native suites and the opt-in E8 benchmark; refuses failed or
missing evidence. This script does not execute tests or rebuild native code.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def counts(path):
    lines = Path(path).read_text().splitlines()
    summary = next((line for line in reversed(lines) if re.search(r'\d+ passed', line)), '')
    if not summary or re.search(r'\d+ (failed|errors?)', summary):
        raise ValueError(f'no passing suite summary in {path}')
    def number(kind):
        found = re.search(rf'(\d+) {kind}', summary)
        return int(found[1]) if found else 0
    return {'passed': number('passed'), 'skipped': number('skipped'), 'subtests': number('subtests')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-log', required=True)
    parser.add_argument('--native-log', required=True)
    parser.add_argument('--benchmark', required=True)
    parser.add_argument('--full-runtime', required=True)
    parser.add_argument('--native-runtime', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    full_runtime = json.loads(Path(args.full_runtime).read_text())
    native_runtime = json.loads(Path(args.native_runtime).read_text())
    for runtime in (full_runtime, native_runtime):
        if (Path(runtime['interpreter']).resolve() != Path(sys.executable).resolve()
                or runtime['python'] != platform.python_version()):
            raise ValueError('evidence generator must match the actual pytest interpreter')
    full, native = counts(args.full_log), counts(args.native_log)
    if native['skipped']:
        raise ValueError('native suite must have zero skips')
    rows = json.loads(Path(args.benchmark).read_text())
    gates = {}
    for name, budget in [('cold', 100_000), ('warm', 10_000)]:
        row, = [r for r in rows if r['experiment'] == 'E8' and r['scale'] == 10000
                and r['operation'] == f'dependency closure {name}']
        if row['unit'] != 'us' or row['iterations'] < 20 or row['p95'] > budget:
            raise ValueError(f'{name} closure target not met: {row}')
        gates[name] = {'p95_ms': row['p95'] / 1000, 'samples': row['iterations'], 'budget_ms': budget / 1000}
    record = json.loads((ROOT / 'mork/FFI_PROVENANCE.json').read_text())
    native_digest = hashlib.sha256(Path(record['reference_artifact']['resolved_path']).read_bytes()).hexdigest()
    patch_digest = hashlib.sha256((ROOT / 'mork/fork-patches/ffi-query-lifetime.patch').read_bytes()).hexdigest()
    if native_digest != record['reference_artifact']['sha256'] or patch_digest != record['sources']['ffi_patch_sha256']:
        raise ValueError('fixed native baseline drifted')
    data = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
            'interpreter': sys.executable, 'platform': platform.platform(),
            'runtime_metadata': {'full': full_runtime, 'native': native_runtime},
            'scope': 'persistent-atp PoC only; D1-D6 accepted in docs/poc-decisions.md',
            'native_changes': 'none during accepted-decisions implementation; library/patch hashes unchanged',
            'full_python': full, 'strict_native_python': native, 'performance': gates,
            'benchmark': str(Path(args.benchmark)), 'native_build': record,
            'remote_ci': 'not observed', 'fresh_network_clone': 'not run',
            'unrelated_skips': 'artifact PostgreSQL integration requires ARTIFACT_TEST_DB_URL'}
    Path(args.out).write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({'full': full, 'native': native, 'performance': gates, 'python': data['python']}, indent=2))


if __name__ == '__main__':
    main()
