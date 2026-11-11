"""Versioned exact identity; legacy records remain readable, never relabelled."""
from collections.abc import Mapping, Sequence
from typing import Any

from commit_gate.canon import content_hash

SERIALIZATION_VERSION = 1


def state_payload(goal_text: str, local_context: Sequence[str], environment_hash: str,
                  serialization_version: int = SERIALIZATION_VERSION) -> dict:
    if type(serialization_version) is not int or serialization_version != SERIALIZATION_VERSION:
        raise ValueError('unsupported state serialization version')
    if not isinstance(goal_text, str) or not isinstance(local_context, Sequence) or isinstance(local_context, (str, bytes)):
        raise ValueError('goal/context must be text and an ordered sequence')
    if not all(isinstance(entry, str) for entry in local_context):
        raise ValueError('local context entries must be text')
    return {'serialization_version': serialization_version, 'goal_text': goal_text,
            'local_context': list(local_context), 'environment_hash': environment_hash}


def exact_state_hash(goal_text: str, local_context: Sequence[str], environment_hash: str,
                     serialization_version: int = SERIALIZATION_VERSION) -> str:
    return content_hash(state_payload(goal_text, local_context, environment_hash, serialization_version))


def identity_issue(state: Mapping[str, Any], environment_hash: str | None = None) -> str | None:
    """None permits current exact reuse; a diagnostic never mutates history."""
    if 'serialization_version' not in state:
        return 'legacy-state-identity'
    try:
        expected = exact_state_hash(state['goal_text'], state['local_context'], state['environment_hash'],
                                    state['serialization_version'])
    except (KeyError, TypeError, ValueError):
        return 'unsupported-or-incomplete-state-identity'
    if state.get('context_digest') != content_hash(state['local_context']):
        return 'context-digest-mismatch'
    if expected != state.get('exact_state_hash'):
        return 'exact-state-hash-mismatch'
    if environment_hash is not None and state['environment_hash'] != environment_hash:
        return 'state-environment-mismatch'
    return None
