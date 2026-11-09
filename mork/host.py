"""Executable persistent-atp PoC projection host and recovery bootstrap.

Run through scripts/with-mork.sh so the fixed native dependency is preloaded.
This host follows committed journal events; it does not accept worker requests,
execute ATP, schedule work or append proposals on a worker's behalf.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from commit_gate import CommitGate, JournalStore
from .backend import MorkSpace, MorkView, ProjectionUnavailable
from .snapshot import create_backup, restore_backup
from .supervisor import Supervisor


class ProofHost:
    def __init__(self, journal: str, readiness: str, snapshot: str | None = None):
        self.readiness = Path(readiness)
        self.readiness.parent.mkdir(parents=True, exist_ok=True)
        self.readiness.unlink(missing_ok=True)
        self.store = JournalStore(journal)
        self.space = MorkSpace()
        self.view = MorkView(self.space)
        self.gate = CommitGate(self.view, self.store)
        self.snapshot = snapshot
        self.verified_heads = {}

    def start(self) -> dict:
        for proof in self.store.proof_ids():
            self.store.verify_chain(proof)
        if self.snapshot:
            restore_backup(self.space, self.view, self.store, self.snapshot)
        self.gate.catch_up()
        if self.snapshot:
            # Rebuild Python dependency indexes before advertising restored
            # proofs. Snapshot files deliberately contain no Python caches.
            from .dependencies import hydrate
            from .atoms import encode
            for proof in self.store.proof_ids():
                if self.space.match(f'(node {encode(proof)} $id "Alignment")', '$id'):
                    hydrate(self.space, proof)
        return self.tick()

    def tick(self) -> dict:
        """Withdraw readiness before repair; publish only at all current heads."""
        if self.space.unavailable:
            self.readiness.unlink(missing_ok=True)
            raise ProjectionUnavailable('native Space unavailable; restart and replay required')
        heads = {proof: self.store.head(proof) for proof in self.store.proof_ids()}
        if heads != self.verified_heads or self.space.invalid_proofs:
            self.readiness.unlink(missing_ok=True)
            for proof in heads:
                if heads[proof] != self.verified_heads.get(proof):
                    self.store.verify_chain(proof)
            self.gate.catch_up()  # includes proofs created after snapshot/startup
        with self.space.lock:
            proofs = {proof: {'journal_revision': self.store.head(proof)[0],
                              'projected_revision': self.view.projected_revision(proof)}
                      for proof in self.store.proof_ids()}
            ready = not self.space.unavailable and not self.space.invalid_proofs and all(
                p['journal_revision'] == p['projected_revision'] for p in proofs.values())
            state = {'ready': ready, 'pid': os.getpid(), 'proofs': proofs}
            if ready:
                self.verified_heads = heads
                temporary = self.readiness.with_name(self.readiness.name + f'.{os.getpid()}.tmp')
                temporary.write_text(json.dumps(state, sort_keys=True) + '\n')
                temporary.replace(self.readiness)
            else:
                self.readiness.unlink(missing_ok=True)
        return state

    def close(self):
        self.readiness.unlink(missing_ok=True)
        self.store.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--journal', required=True, help='authoritative SQLite journal')
    result.add_argument('--ready', required=True, help='readiness JSON for the live child')
    result.add_argument('--snapshot', help='validate/restore this bundle before replay')
    result.add_argument('--snapshot-out', help='publish a new bundle after initial catch-up')
    result.add_argument('--poll-interval', type=float, default=.05)
    result.add_argument('--restart-delay', type=float, default=.25)
    result.add_argument('--once', action='store_true', help='check startup and print status, then exit')
    result.add_argument('--supervise', action='store_true', help='restart child on exit; never serve before recovery')
    return result


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    if args.poll_interval <= 0 or args.restart_delay < 0:
        parser().error('poll interval must be positive; restart delay must be non-negative')
    if args.supervise:
        if args.once or args.snapshot_out:
            parser().error('--supervise cannot combine with --once or --snapshot-out')
        child = [sys.executable, '-m', 'mork.host', *[a for a in argv if a != '--supervise']]
        supervisor = Supervisor(child, args.ready, restart_delay=args.restart_delay)
        signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
        try:
            supervisor.run()
        except KeyboardInterrupt:
            supervisor.stop()
        finally:
            supervisor.stop()
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    host = None
    try:
        host = ProofHost(args.journal, args.ready, args.snapshot)
        status = host.start()
        if args.snapshot_out:
            create_backup(host.space, host.view, host.store, args.snapshot_out)
        if args.once:
            print(json.dumps(status, sort_keys=True))
            return 0 if status['ready'] else 1
        while not stop.wait(args.poll_interval):
            host.tick()
        return 0
    except Exception as exc:
        # Fatal errors withdraw readiness and exit, so supervision can replace
        # a poisoned process. No event is resubmitted or appended here.
        Path(args.ready).unlink(missing_ok=True)
        print(f'persistent-atp host: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 70
    finally:
        if host is not None:
            host.close()


if __name__ == '__main__':
    raise SystemExit(main())
