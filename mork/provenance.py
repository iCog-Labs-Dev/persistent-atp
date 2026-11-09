"""Explicit recording and read-only verification of native build provenance.

The committed record pins source/build identity. A reference artifact hash is
checked when loading that exact path; other machines produce their own runtime
attestation because native binaries include platform/build-path differences.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from .backend.ffi import MorkSpace

REPO = Path(__file__).resolve().parents[1]
RECORD = REPO / 'mork' / 'FFI_PROVENANCE.json'


def configured_build() -> dict:
    return json.loads((REPO / 'mork/FFI_BUILD_CONFIG.json').read_text())


def source_pins() -> dict:
    script = (REPO / 'scripts' / 'with-mork.sh').read_text()
    pins = {key: re.search(rf'^{variable}=(\S+)$', script, re.MULTILINE).group(1)
            for key, variable in [('mork', 'mork_commit'), ('pathmap', 'pathmap_commit'),
                                  ('mork_ffi', 'ffi_source')]}
    pins['ffi_patch_sha256'] = hashlib.sha256((REPO / 'mork/fork-patches/ffi-query-lifetime.patch').read_bytes()).hexdigest()
    return pins


def attest(space: MorkSpace) -> dict:
    return {'resolved_path': space.library_path,
            'sha256': hashlib.sha256(Path(space.library_path).read_bytes()).hexdigest(),
            'build_info': space.build_info(), 'build_configuration': configured_build()}


def verify(record: dict, actual: dict, pins: dict | None = None) -> None:
    pins = pins or source_pins()
    if record.get('build_configuration') != configured_build():
        raise ValueError('configured feature/build policy differs from committed provenance')
    if info_patch := actual['build_info'].get('patch_sha256'):
        if info_patch != pins['ffi_patch_sha256']:
            raise ValueError('loaded FFI patch differs from recorded patch')
    else:
        raise ValueError('loaded library lacks FFI lifetime patch identity')
    if record.get('sources') != pins:
        raise ValueError('committed provenance source pins differ from launcher')
    info = actual['build_info']
    if info.get('mork_commit') != pins['mork']:
        raise ValueError('loaded library source commit differs from pinned fork')
    expected = record.get('build_info')
    if expected != info:
        raise ValueError('loaded library build info/toolchain differs from committed provenance')
    reference = record.get('reference_artifact', {})
    if (reference.get('resolved_path') == actual['resolved_path']
            and reference.get('sha256') != actual['sha256']):
        raise ValueError('reference library SHA256 changed')


def main() -> int:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    try:
        actual = attest(MorkSpace())
        pins = source_pins()
        if actual['build_info'].get('mork_commit') != pins['mork']:
            raise ValueError('loaded library source commit differs from pinned fork')
        if actual['build_info'].get('patch_sha256') != pins['ffi_patch_sha256']:
            raise ValueError('loaded library differs from the required FFI patch')
        if args.record:
            record = {'version': 2, 'library': 'libmork_ffi.so', 'sources': pins,
                      'toolchain': actual['build_info']['rustc'],
                      'build_info': actual['build_info'],
                      'build_configuration': configured_build(),
                      'reference_artifact': {k: actual[k] for k in ('resolved_path', 'sha256')}}
            temporary = RECORD.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
            temporary.replace(RECORD)
        else:
            verify(json.loads(RECORD.read_text()), actual, pins)
        print('with-mork: ' + json.dumps(actual, sort_keys=True), file=sys.stderr)
        return 0
    except (ValueError, OSError) as exc:
        print(f'with-mork: provenance verification failed: {exc}. '
              'Review the build, then explicitly use --record-provenance.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
