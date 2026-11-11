"""Prepare a new formal run's proposal operations; the commit gate owns writes.

This is ingestion for an initial result, not an allocator/resume state machine.
The caller supplies an existing declaration/environment binding and a lease.
"""
from commit_gate.canon import content_hash
from commit_gate.ops import AddEdge, UpsertNode
from .schemas import validate
from .soundness import validate_formal_search_result
from .state_identity import identity_issue


def result_operations(result: dict, *, environment_id: str, actor: str) -> tuple:
    errors = validate('formal-search-result', result)
    if errors:
        raise ValueError('; '.join(errors))
    errors.extend(str(issue) for issue in validate_formal_search_result(result))
    errors.extend(issue for state in result['states'] if (issue := identity_issue(state, result['environment_hash'])))
    if result.get('certificate') and not result['certificate'].get('witness'):
        errors.append('new certificate requires an explicit witness')
    if errors:
        raise ValueError('; '.join(errors))
    proof, run = result['proof_id'], result['run_id']
    declaration = result.get('declaration_id')
    if not declaration or not environment_id.startswith(proof + '/'):
        raise ValueError('initial result requires declaration and proof-scoped environment')
    ops = [UpsertNode('FormalRun', run, {'status': result['disposition'],
                                       'environment_hash': result['environment_hash']})]
    def edge(rel, src, dst, **fields):
        identity = content_hash([rel, src, dst, fields]).split(':')[1]
        ops.append(AddEdge(rel, src, dst, f'{proof}/e-{identity}', fields))
    edge('SEARCHES', run, declaration)
    edge('RAN_UNDER', run, environment_id)
    for state in result['states']:
        ops.append(UpsertNode('FormalState', state['state_id'],
                              {k: v for k, v in state.items() if k != 'state_id'}))
    if result.get('root_state_id'):
        edge('HAS_ROOT', run, result['root_state_id'])
    for tactic in result['tactic_edges']:
        ops.append(UpsertNode('TacticApplication', tactic['tactic_id'],
                              {k: v for k, v in tactic.items()
                               if k not in {'tactic_id', 'source_state_id', 'produced_goal_ids'}}))
        edge('HAS_TACTIC', tactic['source_state_id'], tactic['tactic_id'])
        edge('RUN_OF', tactic['tactic_id'], run)
        for index, child in enumerate(tactic['produced_goal_ids']):
            edge('FORMAL_REQUIRES', tactic['tactic_id'], child, child_index=index)
    if result.get('checkpoint') is not None:
        serial = int(content_hash(run).split(':')[1][:16], 16) + 1
        checkpoint = f'{proof}/fc-{serial}'
        ops.append(UpsertNode('FormalCheckpoint', checkpoint, result['checkpoint']))
        edge('HAS_CHECKPOINT', run, checkpoint)
        for state_id in result['checkpoint']['frontier_state_ids']:
            edge('CHECKPOINT_FRONTIER', checkpoint, state_id)
    if certificate := result.get('certificate'):
        cert_id = certificate['witness']['certificate_id']
        ops.append(UpsertNode('Certificate', cert_id,
                              {**certificate, 'environment_hash': result['environment_hash'],
                               'actor': actor, 'root_state_id': result['root_state_id']}))
        edge('PRODUCED_CERTIFICATE', run, cert_id)
        edge('CERTIFIES', cert_id, declaration)
        edge('CERTIFICATE_ENVIRONMENT', cert_id, environment_id)
    for obstruction in result.get('obstructions', []):
        identity = obstruction['obstruction_id']
        ops.append(UpsertNode('Obstruction', identity,
                              {k: v for k, v in obstruction.items() if k != 'obstruction_id'}))
        edge('RAISED_OBSTRUCTION', run, identity)
        for state_id in obstruction.get('formal_state_ids', []):
            edge('AT_STATE', identity, state_id)
    return tuple(ops)
