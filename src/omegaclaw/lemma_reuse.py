"""One explicit, independently verified dependency in a source-backed proof.

The frozen provider already checks source prefixes in both search and replay.
Only its complete native replay source is reusable here; graph status alone,
an informal suggestion, or an unaudited proof string is never sufficient.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import tempfile

import hashlib


def _hash(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*\Z")


def _node(service, identity, label):
    node = service.view.node(identity)
    if node is None or node.label != label:
        raise ValueError(f'lemma reuse requires a committed {label}')
    return node


def _artifact(service, ref):
    raw = service.artifacts.get(ref)
    if _hash(raw) != ref:
        raise ValueError('lemma reuse artifact content address differs')
    return raw


def verified_lemma(service, proof, claim_id, *, certificate_id=None, replay_id=None):
    """Read and validate a bounded source/replay binding from committed state."""
    if not claim_id.startswith(proof + '/'):
        raise ValueError('reused lemma must belong to the parent proof')
    claim = _node(service, claim_id, 'Claim')
    if claim.fields.get('status') != 'lean-verified':
        raise ValueError('reused lemma must be independently Lean verified')
    certificates = sorted({e.dst_id for e in service.view.edges_from(claim_id, 'PROVED_BY')})
    if certificate_id is None:
        certificate_id = next((key for key in certificates if
            _node(service, key, 'Certificate').fields.get('status') == 'replay-accepted'), None)
    if certificate_id not in certificates:
        raise ValueError('reused lemma has no bound replay-accepted certificate')
    certificate = _node(service, certificate_id, 'Certificate')
    fields = certificate.fields
    if fields.get('status') != 'replay-accepted' or fields.get('protocol_version') != 2:
        raise ValueError('reused lemma certificate is not replay accepted')
    declarations = {e.dst_id for e in service.view.edges_from(certificate_id, 'CERTIFIES')}
    if len(declarations) != 1:
        raise ValueError('reused lemma needs exactly one certified declaration')
    declaration_id = next(iter(declarations))
    declaration = _node(service, declaration_id, 'FormalDeclaration')
    name = declaration.fields.get('lean_name')
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError('reused lemma needs a simple Lean declaration name')
    if (fields.get('source_declaration') != name or fields.get('input_kind') != 'source'
            or declaration.fields.get('lean_source_artifact') != fields.get('source_artifact_hash')):
        raise ValueError('reused lemma source binding differs from its declaration')
    original_source = _artifact(service, fields['source_artifact_hash']).decode('utf-8')
    certificate_data = json.loads(_artifact(service, fields['artifact_hash']))
    input_manifest = json.loads(_artifact(service, fields['input_artifact_hash']))
    if certificate_data.get('format') != 'maths-ai-accepted-certificate-v1':
        raise ValueError('lemma reuse requires the native accepted certificate format')
    if (input_manifest.get('format') != 'maths-ai-formal-input-v1'
            or input_manifest.get('kind') != 'source'
            or input_manifest.get('input') != {'kind': 'source',
                'artifact_hash': fields['source_artifact_hash'], 'declaration': name,
                'starting_state': 'binders-introduced'}
            or input_manifest.get('source_artifact_hash') != fields['source_artifact_hash']
            or input_manifest.get('environment_hash') != fields['environment_hash']
            or input_manifest.get('native_target') != certificate_data.get('native_target')
            or input_manifest.get('statement') != certificate_data.get('statement')):
        raise ValueError('reused lemma certificate differs from its admitted source input')
    alignments = [service.view.node(e.src_id) for e in service.view.edges_to(declaration_id, 'ALIGNS_DECLARATION')]
    alignment = next((a for a in alignments if a is not None and a.label == 'Alignment'
        and a.fields.get('verdict') == 'aligned' and a.fields.get('lifecycle') == 'reviewed'
        and a.fields.get('source_hash') == fields['source_artifact_hash']
        and any(e.dst_id == claim_id for e in service.view.edges_from(a.node_id, 'ALIGNS_CLAIM'))), None)
    if alignment is None:
        raise ValueError('reused lemma lacks exact source-bound claim alignment')
    replay_ids = sorted({e.dst_id for e in service.view.edges_from(certificate_id, 'REPLAYED_BY')})
    if replay_id is None:
        replay_id = next((key for key in replay_ids if _node(service, key, 'LeanReplay').fields.get('status') == 'verified'), None)
    if replay_id not in replay_ids:
        raise ValueError('reused lemma lacks committed independent replay')
    replay = _node(service, replay_id, 'LeanReplay')
    receipt = json.loads(_artifact(service, replay.fields['receipt_artifact']))
    if (replay.fields.get('actor') == fields.get('actor') or replay.fields.get('status') != 'verified'
            or replay.fields.get('sorry_detected') is not False or receipt.get('status') != 'verified'
            or receipt.get('protocol_version') != 2 or replay.fields.get('protocol_version') != 2):
        raise ValueError('reused lemma replay is not independent and verified')
    for key in ('environment_hash', 'input_artifact_hash', 'source_artifact_hash'):
        if receipt.get(key) != fields.get(key) or replay.fields.get(key) != fields.get(key):
            raise ValueError('reused lemma replay bindings differ')
    if (receipt.get('certificate_hash') != fields['artifact_hash']
            or receipt.get('run_id') != fields['producer_run_id']
            or replay.fields.get('certificate_id') != certificate_id
            or replay.fields.get('artifact_hash') != fields['artifact_hash']
            or replay.fields.get('run_id') != fields['producer_run_id']):
        raise ValueError('reused lemma replay certificate/run binding differs')
    cost = receipt.get('verification_cost') or {}
    audit = cost.get('axiom_audit') or {}
    source = audit.get('source')
    if (cost.get('sorry_detected') is not False or cost.get('session') != 'fresh-native-compilation'
            or audit.get('status') != 'passed' or audit.get('exit_code') != 0
            or audit.get('forbidden_axioms') != [] or not isinstance(source, str) or not source.strip()):
        raise ValueError('reused lemma requires complete successful native replay source')
    target = certificate_data.get('native_target') or {}
    steps = certificate_data.get('steps')
    header = f"theorem {name} : {declaration.fields.get('lean_type')}"
    expected_draft = f'import Init\n\n{header} := by\n  exact persistent_atp_pending_proof\n'
    if (original_source != expected_draft or target.get('declaration') != name
            or target.get('header', '').strip() != header or target.get('prefix', '').strip()
            or not isinstance(steps, list) or not steps
            or any(not isinstance(step, str) or not step.strip() for step in steps)):
        raise ValueError('initial reuse requires a standalone Init intake declaration and native proof path')
    body = '\n'.join('  focus\n' + '\n'.join('    ' + line for line in step.splitlines()) for step in steps)
    checked_proof = f'{header} := by\n{body}\n'
    if checked_proof not in source:
        raise ValueError('native replay source differs from the certificate proof path')
    # Reuse only the checked declaration. Native audit imports/guards/printing
    # are not part of the theorem and need not enter the parent's source.
    reusable_source = 'import Init\n\n' + checked_proof
    pins = service.view.edges_from(declaration_id, 'PINNED_ENVIRONMENT')
    if len(pins) != 1:
        raise ValueError('reused lemma needs one environment pin')
    environment = _node(service, pins[0].dst_id, 'Environment')
    if environment.fields.get('environment_hash') != fields['environment_hash']:
        raise ValueError('reused lemma environment pin differs')
    return {'format': 'persistent-atp-verified-lemma-v1', 'proof_id': proof, 'claim_id': claim_id,
        'certificate_id': certificate_id, 'replay_id': replay_id, 'declaration_id': declaration_id,
        'lean_name': name, 'lean_type': declaration.fields.get('lean_type'),
        'environment_hash': fields['environment_hash'], 'source': reusable_source,
        'native_replay_source_hash': _hash(source.encode()),
        'records': [{'id': n.node_id, 'label': n.label, 'fields': deepcopy(dict(n.fields))}
                    for n in (claim, certificate, declaration, alignment, replay, environment)]}


def validate_binding(service, binding):
    current = verified_lemma(service, binding['proof_id'], binding['claim_id'],
        certificate_id=binding['certificate_id'], replay_id=binding['replay_id'])
    if current != binding:
        raise ValueError('saved verified lemma changed; reconcile the original parent input')


def prefix_source(binding, *, mode):
    """Use the complete checked proof; optional simp registration is heuristic."""
    if mode not in {'source-prefix', 'simp-prefix'}:
        raise ValueError('unsupported verified lemma reuse mode')
    suffix = f"\nattribute [local simp 2000] {binding['lean_name']}\n" if mode == 'simp-prefix' else '\n'
    return binding['source'].rstrip() + suffix


def audit_parent_usage(source, parent_name, lemma_name, *, lean, timeout):
    """Fresh Lean checks the actual retained proof value for the lemma constant."""
    if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in (parent_name, lemma_name)):
        raise ValueError('parent usage audit requires simple Lean declaration names')
    binary = Path(lean)
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError('parent usage audit requires an explicitly installed Lean executable')
    check = ('\nopen Lean in\nrun_elab do\n'
        f'  let info ← getConstInfo `{parent_name}\n'
        '  let .thmInfo theoremInfo := info | throwError "PARENT_PROOF_MISSING"\n'
        f'  unless theoremInfo.value.getUsedConstants.contains `{lemma_name} do\n'
        '    throwError "VERIFIED_LEMMA_NOT_USED"\n'
        '  logInfo "PERSISTENT_ATP_LEMMA_USED"\n')
    with tempfile.TemporaryDirectory(prefix='patp-lemma-use-') as directory:
        path = Path(directory) / 'Parent.lean'
        path.write_text('import Lean\n' + source + check)
        result = subprocess.run([str(binary), str(path)], capture_output=True, text=True, timeout=timeout)
    output = (result.stdout + result.stderr)[-16000:]
    return {'format': 'persistent-atp-lemma-use-audit-v1', 'parent_name': parent_name,
        'lemma_name': lemma_name, 'source_hash': _hash(source.encode()),
        'lean_binary_hash': _hash(binary.read_bytes()), 'exit_code': result.returncode,
        'used': result.returncode == 0 and 'PERSISTENT_ATP_LEMMA_USED' in output,
        'diagnostics': output}
