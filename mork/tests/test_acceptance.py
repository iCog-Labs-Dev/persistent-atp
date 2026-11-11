"""Synthetic representation acceptance through the real gate and MORK.

FakeFormalATP supplies traces/replay verdicts; these are not Lean verification.
Witness selection below is an explicit upstream fixture, never chosen by MORK.
"""
from contextlib import closing

import pytest

from commit_gate import CommitGate, JournalStore
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from shared.vocab import WorkerClass
from context_compiler.contracts import CompileRequest, PacketKind, TextBudget
from context_compiler.snapshot import ProofSnapshot
from context_compiler.sources import RetrievalPlan, collect_sources
from mathproof.formal_atp import FakeFormalATP, build_result, build_tactic_edge, stub_replay
from mathproof.witness import build_witness
from mork.backend import MorkSpace, MorkView, MorkUnavailable

ENV = 'sha256:' + '11' * 32


class Scenario:
    def __init__(self, proof, space, store):
        self.proof, self.view, self.store = proof, MorkView(space), store
        self.gate = CommitGate(self.view, store)
        self.token = store.acquire_lease(proof, 'acceptance', actor='coordinator', worker_class='coordinator')
        self.request = dict(proof_id=proof, run_id=self.id('run'), claim_id=self.id('claim'),
                            formal_declaration_id=self.id('decl'), base_revision=0,
                            lease_id='acceptance', fencing_token=self.token,
                            lean_source_artifact='sha256:' + '22' * 32,
                            environment_id=self.id('env'), environment_hash=ENV, goal_text='P ∧ P')
        trace = build_result('budget-exhausted', self.request)
        # Identical goals remain distinct obligations with distinct positions.
        trace['states'][2].update({k: trace['states'][1][k]
                                 for k in ('goal_text', 'exact_state_hash', 'semantic_signature', 'local_context', 'context_digest', 'environment_hash', 'serialization_version')})
        for serial, state in enumerate(trace['states'][1:], 2):
            trace['tactic_edges'].append(build_tactic_edge(proof, serial, state['state_id'],
                                                          'lean-accepted', 0, []))
        candidate = build_result('proved-pending-replay', self.request)['certificate']
        candidate.pop('witness', None)
        trace.update(disposition='proved-pending-replay', certificate=candidate)
        trace['certificate']['witness'] = build_witness(trace, self.id('cert'),
            {t['source_state_id']: t['tactic_id'] for t in trace['tactic_edges']})
        self.atp = FakeFormalATP({self.id('run'): [trace]}, replay_fn=stub_replay('verified'))
        self.result = self.atp.formal_search_start(self.request)
        assert self.result['disposition'] == 'proved-pending-replay'
        self.populate()

    def id(self, local):
        return self.proof + '/' + local

    def node(self, label, local, **fields):
        return UpsertNode(label, self.id(local), fields)

    def edge(self, relation, src, dst, **fields):
        return AddEdge(relation, self.id(src), self.id(dst),
                       self.id(f'e-{relation}-{src}-{dst}'), fields)

    def proposal(self, ops):
        return Proposal(proof_id=self.proof, actor='coordinator', worker_class='coordinator',
                        base_revision=self.store.head(self.proof)[0], lease_id='acceptance',
                        fencing_token=self.token, ops=ops)

    def commit(self, *ops):
        result = self.gate.commit(self.proposal(ops))
        assert result.accepted, result.rejections
        return result

    def populate(self):
        ops = [self.node('Proof', 'proof'), self.node('Claim', 'claim', status='formally-closed', statement='P ∧ P'),
               self.node('Alignment', 'alignment', lifecycle='reviewed', verdict='aligned'),
               self.node('FormalDeclaration', 'decl'), self.node('Environment', 'env', environment_hash=ENV),
               self.node('FormalRun', 'run', status='proved-pending-replay', environment_hash=ENV),
               self.node('FormalCheckpoint', 'checkpoint', artifact_hash='sha256:' + '33' * 32),
               self.node('Certificate', 'cert', **self.result['certificate'], actor='producer',
                         environment_hash=ENV, root_state_id=self.result['root_state_id']),
               self.edge('HAS_TARGET', 'proof', 'claim'),
               self.edge('ALIGNS_CLAIM', 'alignment', 'claim'),
               self.edge('ALIGNS_DECLARATION', 'alignment', 'decl'),
               self.edge('SEARCHES', 'run', 'decl'), self.edge('RAN_UNDER', 'run', 'env'),
               self.edge('PINNED_ENVIRONMENT', 'decl', 'env'),
               self.edge('HAS_CHECKPOINT', 'run', 'checkpoint'),
               self.edge('PRODUCED_CERTIFICATE', 'run', 'cert'),
               self.edge('CERTIFIES', 'cert', 'decl'), self.edge('CERTIFICATE_ENVIRONMENT', 'cert', 'env'),
               self.edge('PROVED_BY', 'claim', 'cert')]
        for state in self.result['states']:
            ops.append(UpsertNode('FormalState', state['state_id'],
                                  {k: v for k, v in state.items() if k != 'state_id'}))
        for tactic in self.result['tactic_edges']:
            local = tactic['tactic_id'].split('/', 1)[1]
            source = tactic['source_state_id'].split('/', 1)[1]
            ops.extend([self.node('TacticApplication', local, executor_result=tactic['executor_result'],
                                  subgoal_count=tactic['subgoal_count'], selected=True),
                        self.edge('HAS_TACTIC', source, local), self.edge('RUN_OF', local, 'run')])
            for index, child in enumerate(tactic['produced_goal_ids']):
                ops.append(self.edge('FORMAL_REQUIRES', local, child.split('/', 1)[1], child_index=index))
        ops.extend([self.edge('HAS_ROOT', 'run', 'fs-1'),
                    self.node('TacticApplication', 'alternative', subgoal_count=0,
                              executor_result='lean-accepted', selected=False),
                    self.edge('HAS_TACTIC', 'fs-1', 'alternative'),
                    self.node('Obstruction', 'obstruction', kind='missing-lemma'),
                    self.edge('AT_STATE', 'obstruction', 'fs-2'),
                    self.edge('RAISED_OBSTRUCTION', 'run', 'obstruction'),
                    self.node('ResearchState', 'research'), self.node('ResearchMove', 'bridge-move'),
                    self.node('Claim', 'lemma', statement='P'), self.node('FormalDeclaration', 'lemma-decl'),
                    self.node('Alignment', 'lemma-alignment', lifecycle='reviewed', verdict='aligned'),
                    self.edge('PROPOSES', 'research', 'bridge-move'),
                    self.edge('MOVE_TARGETS', 'bridge-move', 'lemma'),
                    self.edge('RESOLVES', 'lemma', 'obstruction'),
                    self.edge('ALIGNS_CLAIM', 'lemma-alignment', 'lemma'),
                    self.edge('ALIGNS_DECLARATION', 'lemma-alignment', 'lemma-decl'),
                    self.edge('DEPENDS_ON', 'claim', 'lemma'),
                    self.edge('DEPENDS_ON', 'lemma', 'claim'),
                    self.node('Context', 'context', artifact_hash='sha256:' + '44' * 32),
                    self.edge('IN_CONTEXT', 'context', 'fs-1')])
        self.commit(*ops)
        self.commit(SetField('Certificate', self.id('cert'), 'status', 'replay-pending', 'candidate'))

    def replay(self, sorry=False, environment=ENV):
        verdict = self.atp.formal_replay(self.result['certificate'], environment)
        return self.proposal([
            self.node('LeanReplay', 'replay', **verdict, actor='independent-replayer',
                      sorry_detected=sorry, certificate_id=self.id('cert'),
                      artifact_hash=self.result['certificate']['artifact_hash']),
            self.edge('REPLAYED_BY', 'cert', 'replay'), self.edge('REPLAY_ENVIRONMENT', 'replay', 'env'),
            SetField('Certificate', self.id('cert'), 'status', 'replay-accepted', 'replay-pending'),
        ])


@pytest.fixture
def scenario(request):
    try:
        space = MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    with closing(JournalStore()) as store:
        yield Scenario('accept-' + request.node.name, space, store)


def test_chain_one_verified_evidence_and_witness(scenario):
    s = scenario
    replay = s.gate.commit(s.replay())
    assert replay.accepted, replay.rejections
    s.commit(SetField('Claim', s.id('claim'), 'status', 'lean-verified', 'formally-closed'))
    evidence = s.view.get_claim_evidence(s.id('claim'))
    assert evidence.consistent
    assert evidence.claim.fields['status'] == 'lean-verified'
    assert evidence.replays[0].fields['status'] == 'verified'
    assert s.view.get_run(s.id('run'))['checkpoints'][0].node_id == s.id('checkpoint')
    witness = s.view.get_certificate_witness(s.id('cert'))
    assert len(witness.children) == 2 and all(child.closure for child in witness.children)
    children = s.view.get_tactic_children(s.id('ta-1'))
    assert [edge.fields['child_index'] for edge in children] == [0, 1]
    assert children[0].dst_id != children[1].dst_id
    assert s.view.node(children[0].dst_id).fields['exact_state_hash'] == s.view.node(children[1].dst_id).fields['exact_state_hash']
    assert len(s.view.get_outgoing_tactics(s.id('fs-1'))) == 2
    assert len(s.view.get_run_states(s.id('run'))) == 3


def test_chain_two_bridge_cycles_and_stale_impact(scenario):
    s = scenario
    obstruction = s.view.get_obstructions(s.id('fs-2'))[0]
    bridge_claim = s.view.edges_to(obstruction.node_id, 'RESOLVES')[0].src_id
    move = s.view.edges_to(bridge_claim, 'MOVE_TARGETS')[0].src_id
    assert move == s.id('bridge-move')
    assert s.view.get_declaration(bridge_claim).node_id == s.id('lemma-decl')
    assert s.view.get_transitive_dependencies(s.id('decl')) == (s.id('lemma-decl'),)
    assert s.view.get_dependents(bridge_claim) == (s.id('claim'),)
    before = s.view.atoms(s.proof)
    impact = s.view.get_stale_impact(s.id('lemma-decl'))
    assert s.id('claim') in impact and s.id('decl') in impact
    assert s.id('claim') in s.view.get_stale_impact(s.id('cert'))
    assert s.id('decl') in s.view.get_stale_impact(s.id('env'))
    assert s.view.atoms(s.proof) == before  # discovery does not authorize status writes
    assert s.view.get_discovery(s.proof)['research_moves'][0].node_id == move


def test_context_membership_and_shared_source_reader(scenario):
    s = scenario
    revision, snapshot = s.view.snapshot(s.proof)
    request = CompileRequest(task_id='accept', proof_id=s.proof, base_revision=revision,
                             packet_kind=PacketKind.FORMAL, worker_class=WorkerClass.FORMAL_ATP,
                             text_budget=TextBudget(max_tokens=1000))
    sources = collect_sources(request, ProofSnapshot(s.proof, revision, snapshot), object(),
                              RetrievalPlan(kernel_id=s.id('claim'), seed_ids=(s.id('fs-1'),)))
    assert sources.fetch(s.id('fs-1'))['goal_text'] == 'P ∧ P'
    assert s.view.edges_from(s.id('context'), 'IN_CONTEXT')[0].dst_id == s.id('fs-1')


def test_sorry_and_environment_drift_are_rejected(scenario):
    s = scenario
    for proposal in (s.replay(sorry=True), s.replay(environment='sha256:' + '99' * 32)):
        head = s.store.head(s.proof)
        promotion = s.proposal([*proposal.ops,
            SetField('Claim', s.id('claim'), 'status', 'lean-verified', 'formally-closed')])
        result = s.gate.commit(promotion)
        assert not result.accepted
        assert s.store.head(s.proof) == head
        assert s.view.node(s.id('replay')) is None


def test_failed_zero_child_tactic_cannot_close(scenario):
    s = scenario
    result = s.gate.commit(s.proposal([
        s.node('TacticApplication', 'failed', subgoal_count=0,
               executor_result='lean-rejected', status='closed', diagnostic_artifact='sha256:' + '66' * 32),
        s.edge('HAS_TACTIC', 'fs-2', 'failed'), s.edge('CLOSES_STATE', 'failed', 'fs-2'),
    ]))
    assert not result.accepted
    assert Reason.CLOSURE_WITHOUT_LEAN_ACCEPTED in {r.reason for r in result.rejections}


def test_witness_cycle_is_rejected_instead_of_truncated(scenario):
    s = scenario
    # Exploratory cycles are allowed, but a selected certificate cycle fails.
    s.commit(s.node('TacticApplication', 'loop', subgoal_count=1, executor_result='lean-accepted'),
             s.edge('HAS_TACTIC', 'fs-2', 'loop'),
             s.edge('FORMAL_REQUIRES', 'loop', 'fs-1', child_index=0))
    import copy
    record = copy.deepcopy(s.result['certificate']['witness'])
    record['selected_tactics'][s.id('fs-2')] = s.id('loop')
    record['certificate_id'] = s.id('cycle-cert')
    fields = {**s.view.node(s.id('cert')).fields, 'witness': record, 'status': 'candidate'}
    s.commit(s.node('Certificate', 'cycle-cert', **fields),
             s.edge('CERTIFIES', 'cycle-cert', 'decl'),
             s.edge('CERTIFICATE_ENVIRONMENT', 'cycle-cert', 'env'),
             s.edge('PRODUCED_CERTIFICATE', 'run', 'cycle-cert'))
    with pytest.raises(ValueError, match='cycle'):
        s.view.get_certificate_witness(s.id('cycle-cert'))


def test_semantic_peers_preserve_exact_identities(scenario):
    s = scenario
    original = s.view.node(s.id('fs-2'))
    s.commit(s.node('FormalState', 'peer', status='open',
                    semantic_signature=original.fields['semantic_signature'],
                    exact_state_hash='sha256:' + '77' * 32))
    peers = s.view.get_semantic_peers(original.node_id)
    assert [peer.node_id for peer in peers] == [s.id('peer')]
    assert s.view.node(s.id('fs-2')) is not None
    assert s.view.node(s.id('fs-3')) is not None
    assert s.view.node(s.id('peer')) is not None
