"""Adapter-owned, certificate-scoped witness records; no query-time path choice."""
from collections.abc import Mapping
from typing import Any

from commit_gate.vocab import NON_KERNEL_TACTICS
from commit_gate.canon import content_hash

FIELDS = {'certificate_id', 'root_state_id', 'selected_tactics', 'indexed_obligations',
          'closure_leaves', 'membership_set', 'artifact_hash'}


def certificate_id_for_result(result: Mapping[str, Any]) -> str:
    serial = int(content_hash([result['run_id'], result['certificate']['artifact_hash']]).split(':')[1][:16], 16) + 1
    return f"{result['proof_id']}/cert-{serial}"


def build_witness(result: Mapping[str, Any], certificate_id: str,
                  selected_tactics: Mapping[str, str]) -> dict:
    """Serialize an explicitly selected upstream path and validate all obligations."""
    edges = {edge['tactic_id']: edge for edge in result['tactic_edges']}
    obligations, leaves = [], []
    for state_id, tactic_id in selected_tactics.items():
        edge = edges[tactic_id]
        if not edge['produced_goal_ids']:
            leaves.append(state_id)
        obligations.extend({'tactic_id': tactic_id, 'child_index': index, 'state_id': child}
                           for index, child in enumerate(edge['produced_goal_ids']))
    record = {'certificate_id': certificate_id, 'root_state_id': result['root_state_id'],
              'selected_tactics': dict(sorted(selected_tactics.items())),
              'indexed_obligations': sorted(obligations, key=lambda o: (o['tactic_id'], o['child_index'])),
              'closure_leaves': sorted(leaves), 'membership_set': sorted(selected_tactics),
              'artifact_hash': result['certificate']['artifact_hash']}
    validate_witness(record, result, certificate_id=certificate_id)
    return record


def validate_witness(record: Mapping[str, Any], result: Mapping[str, Any], *,
                     certificate_id: str | None = None) -> None:
    """Require exact reachability, selection, arity, indexing and accepted closure."""
    if not isinstance(record, Mapping) or set(record) != FIELDS:
        raise ValueError('witness must contain exactly the seven contract fields')
    from .schemas import validate
    errors = validate('certificate-witness', record)
    if errors:
        raise ValueError('; '.join(errors))
    if certificate_id is not None and record['certificate_id'] != certificate_id:
        raise ValueError('witness certificate identity mismatch')
    if record['root_state_id'] != result.get('root_state_id'):
        raise ValueError('witness root mismatch')
    if record['artifact_hash'] != result['certificate']['artifact_hash']:
        raise ValueError('witness artifact mismatch')
    proof = result.get('proof_id')
    if proof and not str(record['certificate_id']).startswith(proof + '/'):
        raise ValueError('witness certificate has a different proof scope')
    states = {state['state_id'] for state in result['states']}
    edges = {edge['tactic_id']: edge for edge in result['tactic_edges']}
    if len(states) != len(result['states']) or len(edges) != len(result['tactic_edges']):
        raise ValueError('duplicate state/tactic identities in witness input')
    selection = record['selected_tactics']
    if not isinstance(selection, Mapping) or not all(isinstance(s, str) and isinstance(t, str)
                                                    for s, t in selection.items()):
        raise ValueError('witness selection must map state IDs to tactic IDs')
    if proof and any(not s.startswith(proof + '/') or not t.startswith(proof + '/') for s, t in selection.items()):
        raise ValueError('witness selection has a different proof scope')
    reached, active, obligations, leaves = set(), set(), [], set()

    def walk(state_id):
        if state_id in active:
            raise ValueError('cycle in certificate witness')
        if state_id in reached:
            return
        if state_id not in states or state_id not in selection:
            raise ValueError('missing state or selected tactic in witness')
        edge = edges.get(selection[state_id])
        if edge is None or edge.get('source_state_id') != state_id:
            raise ValueError('selected tactic does not belong to witness state')
        children = edge.get('produced_goal_ids')
        if (edge.get('executor_result') != 'lean-accepted'
                or edge.get('tactic_label') in NON_KERNEL_TACTICS
                or not isinstance(children, list)
                or type(edge.get('subgoal_count')) is not int
                or edge['subgoal_count'] != len(children) or len(set(children)) != len(children)):
            raise ValueError('selected tactic is not a complete accepted transition')
        active.add(state_id)
        if not children:
            leaves.add(state_id)
        for index, child in enumerate(children):
            obligations.append({'tactic_id': edge['tactic_id'], 'child_index': index, 'state_id': child})
            walk(child)
        active.remove(state_id)
        reached.add(state_id)
    walk(record['root_state_id'])
    if set(selection) != reached:
        raise ValueError('witness contains unreachable selection')
    for name, expected in [('membership_set', reached), ('closure_leaves', leaves)]:
        values = record[name]
        if not isinstance(values, list) or len(values) != len(set(values)) or set(values) != expected:
            raise ValueError(f'witness {name} does not match selected graph')
    expected = sorted(obligations, key=lambda o: (o['tactic_id'], o['child_index']))
    if record['indexed_obligations'] != expected:
        raise ValueError('witness obligations are missing, extra or out of order')
