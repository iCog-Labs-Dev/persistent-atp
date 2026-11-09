"""Failures must remain invisible across wrappers until journal replay repairs them."""
import threading
from contextlib import closing
from unittest.mock import patch

import pytest

from commit_gate import CommitGate, JournalStore
from commit_gate.ops import SetField, UpsertNode
from commit_gate.proposal import Proposal
from mork.backend.ffi import MorkSpace, MorkUnavailable, MorkError
from mork.backend.view import MorkView, ProjectionUnavailable


def test_partial_mutation_is_blocked_across_views_and_repaired():
    try:
        space = MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    view, reader = MorkView(space), MorkView(MorkSpace())
    proof = 'recovery-transient'
    with closing(JournalStore()) as store:
        for revision, ops in enumerate([
            [UpsertNode('FormalState', proof + '/s', {'depth': 0})],
            [SetField('FormalState', proof + '/s', 'depth', 1, 0),
             UpsertNode('FormalState', proof + '/child', {})],
        ], 1):
            store.append(Proposal(proof_id=proof, actor='test', worker_class='test',
                                  ops=ops).to_dict())
            if revision == 1:
                view.apply_journal_event(store, proof, revision)
        def fail(*args, **kwargs):
            raise MorkError('transient write failure after field update')
        with patch.object(view, 'add_node', side_effect=fail):
            with pytest.raises(MorkError):
                view.apply_journal_event(store, proof, 2)
        assert reader.projected_revision(proof) == 1
        for read in [lambda: reader.node(proof + '/s'), lambda: reader.snapshot(proof),
                     lambda: reader.get_run_states(proof + '/r'), lambda: reader.atoms(proof)]:
            with pytest.raises(ProjectionUnavailable):
                read()
        assert CommitGate(reader, store).catch_up() == {proof: 1}
        assert reader.node(proof + '/s').fields['depth'] == 1
        assert reader.node(proof + '/child') is not None
        assert store.head(proof)[0] == 2


def test_shared_lock_covers_a_complete_query():
    try:
        space = MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    first, second = MorkView(space), MorkView(MorkSpace())
    first.add_node('shared-lock/run', 'FormalRun')
    first.add_node('shared-lock/s', 'FormalState', {'depth': 0})
    first.add_edge('HAS_ROOT', 'shared-lock/run', 'shared-lock/s', 'shared-lock/root')
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []
    original = first.edges_from
    def pause_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    def read():
        try:
            results.extend(first.get_run_states('shared-lock/run'))
        except BaseException as exc:
            errors.append(exc)
    def write():
        second.set_field('shared-lock/s', 'depth', 1)
        finished.set()
    with patch.object(first, 'edges_from', side_effect=pause_after_read):
        reader = threading.Thread(target=read)
        reader.start()
        assert entered.wait(3)
        writer = threading.Thread(target=write)
        writer.start()
        try:
            assert not finished.wait(.02)
        finally:
            release.set()
            reader.join(3)
            writer.join(3)
    assert not errors
    assert results[0].fields['depth'] == 0  # no mixed-revision compound query
    assert second.node('shared-lock/s').fields['depth'] == 1


def test_watermark_publication_failure_restores_previous_mark():
    try:
        space = MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    view = MorkView(space)
    proof = 'recovery-watermark'
    with closing(JournalStore()) as store:
        for index in range(2):
            store.append(Proposal(proof_id=proof, actor='test', worker_class='test',
                         ops=[UpsertNode('FormalState', f'{proof}/s{index}', {})]).to_dict())
        view.apply_journal_event(store, proof, 1)
        add = space.add
        failed = False
        def transient(*atoms):
            nonlocal failed
            if not failed and any(a.startswith('(projected ') and ' 2 ' in a for a in atoms):
                failed = True
                raise MorkError('publication failed')
            return add(*atoms)
        with patch.object(space, 'add', side_effect=transient):
            with pytest.raises(MorkError):
                view.apply_journal_event(store, proof, 2)
        assert view.projected_revision(proof) == 1
        with pytest.raises(ProjectionUnavailable):
            view.nodes(proof)
        CommitGate(view, store).catch_up()
        assert view.projected_revision(proof) == 2
        assert len(view.nodes(proof)) == 2
