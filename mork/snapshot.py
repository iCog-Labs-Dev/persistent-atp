"""Checksummed, journal-bound snapshots of the whole process-wide MORK Space.

Backups require all projections to be caught up. Restore stages a private copy,
checks journal ancestry and a fresh child Space, then swaps under the shared
lock. Any unexpected failure after swapping disables reads until host restart.
The authoritative journal is never modified by snapshot operations.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from commit_gate.canon import GENESIS_HASH
from commit_gate.store import JournalStore
from mork.atoms import PROJECTED_SCHEMA_VERSION
from .backend.ffi import MorkSpace
from .backend.view import MorkView, ProjectionUnavailable, decode

BUNDLE_FORMAT = 'mork-snapshot-bundle'
BUNDLE_VERSION = 1
KERNEL_DIR = 'kernel'
META_NAME = 'bundle.json'
FEATURES = ['kernel-tree', 'symbol-table', 'sha256', 'journal-prefix']


def _proof_ids_in_space(space: MorkSpace) -> set[str]:
    proofs = set()
    for pattern in ('(node $proof $id $label)', '(projected $proof $revision $schema_version)'):
        proofs.update(decode(row) for row in space.match(pattern, '$proof'))
    return proofs


def _digest_atoms(space: MorkSpace) -> str:
    return hashlib.sha256(('\n'.join(sorted(space.atoms())) + '\n').encode()).hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'symlink in snapshot: {path.relative_to(root)}')
        if path.is_file() and path != root / META_NAME:
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _journal_record(store: JournalStore, proof: str) -> dict[str, Any]:
    store.verify_chain(proof)
    chain = store.read_chain(proof)
    revision, digest = (chain[-1][0], chain[-1][1]) if chain else (0, GENESIS_HASH)
    return {'revision': revision, 'schema_version': PROJECTED_SCHEMA_VERSION if revision else 0,
            'journal_revision': revision, 'journal_hash': digest}


def create_backup(space: MorkSpace, view: MorkView, store: JournalStore,
                  directory: str | Path) -> Path:
    """Atomically publish a new directory bundle; never overwrite an old one."""
    root = Path(directory).absolute()
    if root.exists():
        raise FileExistsError(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.mork-backup-', dir=root.parent))
    try:
        with space.lock:
            if space.unavailable or space.invalid_proofs:
                raise ProjectionUnavailable('cannot snapshot an invalid Space')
            represented = _proof_ids_in_space(space)
            proofs = sorted(set(store.proof_ids()) | represented)
            records = {proof: _journal_record(store, proof) for proof in proofs}
            for proof, record in records.items():
                if (view.projected_revision(proof), view.projected_schema_version(proof)) != (
                    record['revision'], record['schema_version']
                ) or (proof in represented and not record['revision']):
                    raise ValueError(f'projection for {proof} is not at its journal head')
            meta = {'format': BUNDLE_FORMAT, 'version': BUNDLE_VERSION,
                    'schema_version': PROJECTED_SCHEMA_VERSION, 'features': FEATURES,
                    'proofs': records, 'atoms_sha256': _digest_atoms(space)}
            space.backup(str(staging / KERNEL_DIR))
            meta['checksums'] = _inventory(staging)
            (staging / META_NAME).write_text(json.dumps(meta, indent=2, sort_keys=True) + '\n')
            # Flush files before publishing the directory name.
            for path in staging.rglob('*'):
                if path.is_file():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            os.rename(staging, root)
            for directory_path in (root, root.parent):
                fd = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return root
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def validate_backup(directory: str | Path) -> list[str]:
    """Validate metadata, complete file inventory and checksums without loading."""
    root = Path(directory)
    try:
        if root.is_symlink():
            raise ValueError('snapshot root is a symlink')
        meta_path = root / META_NAME
        if meta_path.is_symlink():
            raise ValueError('snapshot metadata is a symlink')
        meta = json.loads(meta_path.read_text())
        if not isinstance(meta, dict):
            raise ValueError('bundle metadata must be an object')
        if meta.get('format') != BUNDLE_FORMAT or meta.get('version') != BUNDLE_VERSION:
            raise ValueError('unsupported snapshot format or version')
        if meta.get('schema_version') != PROJECTED_SCHEMA_VERSION:
            raise ValueError('unsupported projection schema')
        if meta.get('features') != FEATURES:
            raise ValueError('unsupported snapshot features')
        if not isinstance(meta.get('proofs'), dict):
            raise ValueError('missing proof watermarks')
        for proof, record in meta['proofs'].items():
            if not isinstance(proof, str) or not isinstance(record, dict):
                raise ValueError('invalid proof record')
            revision = record.get('revision')
            if type(revision) is not int or revision < 0:
                raise ValueError('invalid projection revision')
            if record.get('journal_revision') != revision:
                raise ValueError('snapshot journal and projection revisions differ')
            if record.get('schema_version') != (PROJECTED_SCHEMA_VERSION if revision else 0):
                raise ValueError('invalid proof schema')
            if not isinstance(record.get('journal_hash'), str):
                raise ValueError('missing journal identity')
        checksums = meta.get('checksums')
        if not isinstance(checksums, dict) or not checksums:
            raise ValueError('missing checksums')
        if not (root / KERNEL_DIR / 'meta.json').is_file():
            raise ValueError('missing kernel backup')
        if _inventory(root) != checksums:
            raise ValueError('snapshot checksum or file inventory mismatch')
        if not isinstance(meta.get('atoms_sha256'), str):
            raise ValueError('missing atom digest')
        return []
    except (OSError, ValueError, TypeError) as exc:
        return [str(exc)]


def _check_journal(store: JournalStore, meta: dict) -> dict:
    report = {'consistent': True, 'pending_replay': 0, 'proofs': {}}
    for proof, record in meta['proofs'].items():
        store.verify_chain(proof)
        chain = store.read_chain(proof)
        revision = record['journal_revision']
        if len(chain) < revision:
            raise ValueError(f'journal for {proof} rewound behind snapshot')
        digest = chain[revision - 1][1] if revision else GENESIS_HASH
        if digest != record['journal_hash']:
            raise ValueError(f'journal for {proof} does not descend from snapshot')
        report['proofs'][proof] = {**record, 'journal_head': list(store.head(proof))}
    # Proofs first created after the snapshot also require replay before ready.
    for proof in store.proof_ids():
        store.verify_chain(proof)
        head = store.head(proof)[0]
        report['pending_replay'] += head - meta['proofs'].get(proof, {}).get('revision', 0)
    return report


def _check_space(space: MorkSpace, meta: dict) -> None:
    view = MorkView(space)
    expected_proofs = {p for p, r in meta['proofs'].items() if r['revision']}
    if _proof_ids_in_space(space) != expected_proofs:
        raise ValueError('restored proof set differs from snapshot metadata')
    for proof, record in meta['proofs'].items():
        if (view.projected_revision(proof), view.projected_schema_version(proof)) != (
            record['revision'], record['schema_version']
        ):
            raise ValueError(f'restored watermark mismatch for {proof}')
    if _digest_atoms(space) != meta['atoms_sha256']:
        raise ValueError('restored atoms differ from snapshot digest')


def restore_backup(space: MorkSpace, view: MorkView, store: JournalStore,
                   directory: str | Path) -> dict[str, Any]:
    """Stage and validate in a fresh process before replacing the live Space.

    Call CommitGate.catch_up() for *all* proofs before declaring readiness.
    """
    root = Path(directory).absolute()
    problems = validate_backup(root)
    if problems:
        raise ValueError('; '.join(problems))
    with tempfile.TemporaryDirectory(prefix='mork-restore-') as temporary:
        staged = Path(temporary) / 'bundle'
        shutil.copytree(root, staged, symlinks=True)
        problems = validate_backup(staged)
        if problems:
            raise ValueError('; '.join(problems))
        meta = json.loads((staged / META_NAME).read_text())
        _check_journal(store, meta)
        env = dict(os.environ, MORK_LIBRARY=space.library_path,
                   LD_PRELOAD=space.library_path,
                   PYTHONPATH=os.pathsep.join(str(p) for p in sys.path if p))
        child = subprocess.run([sys.executable, '-m', 'mork.snapshot', str(staged)],
                               env=env, capture_output=True, text=True, timeout=60)
        if child.returncode:
            raise ValueError(f'fresh-Space snapshot validation failed: {child.stderr.strip()}')
        with space.lock:
            if space.unavailable:
                raise ProjectionUnavailable('restart required before restore')
            report = _check_journal(store, meta)
            try:
                space.restore(str(staged / KERNEL_DIR))
                _check_space(space, meta)
            except BaseException:
                # Kernel errors may follow a swap: never let old views read it.
                type(space).unavailable = True
                raise
            space.invalid_proofs.clear()
            space.recovery_targets.clear()
            # A restored prefix is consistent, but public reads stay blocked
            # until each proof reaches the current authoritative journal head.
            for proof in store.proof_ids():
                if view.projected_revision(proof) != store.head(proof)[0]:
                    space.invalid_proofs.add(proof)
                    space.recovery_targets[proof] = store.head(proof)[0]
            return report


if __name__ == '__main__':
    bundle = Path(sys.argv[1])
    metadata = json.loads((bundle / META_NAME).read_text())
    native_space = MorkSpace()
    native_space.restore(str(bundle / KERNEL_DIR))
    _check_space(native_space, metadata)
