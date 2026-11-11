"""Fresh-process persistence scenarios (a second wrapper is not a new Space)."""
import json
import sys
from pathlib import Path

from commit_gate import CommitGate, JournalStore
from commit_gate.ops import UpsertNode, SetField, AddEdge, RemoveEdge
from commit_gate.proposal import Proposal
from mork.backend import MorkSpace, MorkView
from mork.backend.view import ProjectionUnavailable
from mork.projector.core import project_event_journal
from mork.snapshot import create_backup, restore_backup, validate_backup


def append(store, proof, ops):
    store.append(Proposal(proof_id=proof, actor='fixture', worker_class='fixture', ops=ops).to_dict())


def main(mode, temporary):
    root = Path(temporary)
    store = JournalStore(str(root / 'journal.sqlite'))
    space = MorkSpace()
    view = MorkView(space)
    large = {'text': '\\"Unicode λ\n' * 1200, 'numbers': list(range(100))}
    append(store, 'a', [UpsertNode('FormalState', 'a/s', {'depth': 0, 'payload': large}),
                        UpsertNode('FormalState', 'a/child', {}),
                        AddEdge('CHILD_OF', 'a/child', 'a/s', 'a/edge', {})])
    append(store, 'b', [UpsertNode('FormalState', 'b/s', {'depth': 2})])
    if mode == 'dependencies':
        append(store, 'a', [
            UpsertNode('Claim', 'a/c1', {}), UpsertNode('Claim', 'a/c2', {}),
            UpsertNode('FormalDeclaration', 'a/d1', {}), UpsertNode('FormalDeclaration', 'a/d2', {}),
            UpsertNode('Alignment', 'a/al1', {'lifecycle': 'reviewed', 'verdict': 'aligned'}),
            UpsertNode('Alignment', 'a/al2', {'lifecycle': 'reviewed', 'verdict': 'aligned'}),
            AddEdge('ALIGNS_CLAIM', 'a/al1', 'a/c1', 'a/ac1', {}),
            AddEdge('ALIGNS_CLAIM', 'a/al2', 'a/c2', 'a/ac2', {}),
            AddEdge('ALIGNS_DECLARATION', 'a/al1', 'a/d1', 'a/ad1', {}),
            AddEdge('ALIGNS_DECLARATION', 'a/al2', 'a/d2', 'a/ad2', {}),
            AddEdge('DEPENDS_ON', 'a/c1', 'a/c2', 'a/dep', {}),
        ])
    CommitGate(view, store).catch_up()
    before = space.atoms()
    backup = create_backup(space, view, store, root / 'bundle')
    assert not validate_backup(backup)
    assert view.node('a/s').fields['payload'] == large
    if mode == 'dependencies':
        assert view.get_transitive_dependencies('a/d1') == ('a/d2',)
        view.remove_edge('a/dep')
        assert view.get_transitive_dependencies('a/d1') == ()
        restore_backup(space, view, store, backup)
        assert view.get_transitive_dependencies('a/d1') == ('a/d2',)
        view.set_field('a/al2', 'lifecycle', 'superseded')
        assert view.get_transitive_dependencies('a/d1') == ()
    elif mode == 'roundtrip':
        view.add_node('a/uncommitted', 'FormalState')
        restore_backup(space, view, store, backup)
        assert space.atoms() == before
        assert view.node('a/s').fields['payload'] == large
        try:
            create_backup(space, view, store, backup)
        except FileExistsError:
            pass
        else:
            raise AssertionError('overwrote a published bundle')
    elif mode == 'replay':
        append(store, 'a', [SetField('FormalState', 'a/s', 'depth', 1, 0)])
        append(store, 'a', [UpsertNode('FormalState', 'a/new', {})])
        append(store, 'newproof', [UpsertNode('FormalState', 'newproof/s', {})])
        CommitGate(view, store).catch_up()
        expected = space.atoms()
        result = restore_backup(space, view, store, backup)
        assert result['pending_replay'] == 3
        try:
            view.node('a/s')
        except ProjectionUnavailable:
            pass
        else:
            raise AssertionError('lagging restore was readable')
        CommitGate(view, store).catch_up()
        assert space.atoms() == expected
        assert CommitGate(view, store).catch_up() == {}
    elif mode == 'corrupt':
        target = backup / 'kernel' / 'meta.json'
        target.write_text(target.read_text() + 'corrupt')
        assert validate_backup(backup)
        reject_restore(space, view, store, backup, before)
        (backup / 'bundle.json').write_text('[]')
        assert validate_backup(backup)
    elif mode == 'foreign':
        foreign = JournalStore()
        append(foreign, 'a', [UpsertNode('FormalState', 'a/other', {})])
        append(foreign, 'a', [UpsertNode('FormalState', 'a/other2', {})])
        reject_restore(space, view, foreign, backup, before)
        foreign.close()
    elif mode == 'rewound':
        empty = JournalStore()
        reject_restore(space, view, empty, backup, before)
        empty.close()
    elif mode == 'metadata':
        meta_path = backup / 'bundle.json'
        meta = json.loads(meta_path.read_text())
        meta['atoms_sha256'] = '0' * 64
        meta_path.write_text(json.dumps(meta))
        reject_restore(space, view, store, backup, before)
    elif mode == 'parity':
        append(store, 'a', [SetField('FormalState', 'a/s', 'depth', 1, 0),
                            RemoveEdge('CHILD_OF', 'a/edge')])
        CommitGate(view, store).catch_up()
        journal = {'events': [{'revision': index, 'payload': event}
                            for index, event in enumerate(store.read_events('a'), 1)]}
        commands = project_event_journal(journal)
        batch = sorted(c[len('!(add-atom &mork '):-1] for c in commands)
        # Batch files omit operational watermarks; live comparison excludes only those.
        live = [a for a in view.atoms('a') if not a.startswith('(projected ')]
        assert batch == live, (batch, live)
    elif mode == 'panic':
        from mork.backend import MorkError
        try:
            # Deliberately bypass Python's codec to exercise an unrecoverable
            # malformed native expression, isolated from the suite's Space.
            space._command(b'add-atoms', ('(bad (' + 'x ' * 100 + '))').encode())
        except MorkError:
            pass
        else:
            raise AssertionError('native malformed arity did not fail')
        assert space.unavailable
        try:
            MorkView(MorkSpace()).nodes('b')
        except ProjectionUnavailable:
            pass
        else:
            raise AssertionError('a different proof remained readable after panic')
    elif mode == 'concurrent':
        import threading
        from unittest.mock import patch
        entered, appended = threading.Event(), threading.Event()
        errors = []
        original = space.backup
        def delayed(directory):
            entered.set()
            assert appended.wait(3)
            original(directory)
        def writer():
            try:
                assert entered.wait(3)
                concurrent_store = JournalStore(str(root / 'journal.sqlite'))
                append(concurrent_store, 'a', [SetField('FormalState', 'a/s', 'depth', 1, 0)])
                appended.set()
                CommitGate(MorkView(MorkSpace()), concurrent_store).catch_up()
                concurrent_store.close()
            except BaseException as exc:
                errors.append(exc)
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        with patch.object(space, 'backup', side_effect=delayed):
            concurrent = create_backup(space, view, store, root / 'concurrent')
        writer_thread.join(5)
        assert not writer_thread.is_alive() and not errors, errors
        expected = space.atoms()
        meta = json.loads((concurrent / 'bundle.json').read_text())
        assert meta['proofs']['a']['revision'] == 1
        assert restore_backup(space, view, store, concurrent)['pending_replay'] == 1
        CommitGate(view, store).catch_up()
        assert space.atoms() == expected
    elif mode == 'invalid':
        space.invalid_proofs.add('a')
        try:
            create_backup(space, view, store, root / 'bad')
        except ProjectionUnavailable:
            pass
        else:
            raise AssertionError('backed up invalid projection')
        assert not (root / 'bad').exists()
    store.close()


def reject_restore(space, view, store, backup, before):
    try:
        restore_backup(space, view, store, backup)
    except ValueError:
        pass
    else:
        raise AssertionError('bad snapshot was accepted')
    assert space.atoms() == before
    assert view.node('a/s') is not None


if __name__ == '__main__':
    main(*sys.argv[1:])
