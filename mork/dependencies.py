"""Rebuildable Python dependency index for persistent-atp's PoC query budget.

It mirrors only dependency/alignment atoms observed through the Python bridge.
Restore or an uncertain native mutation discards it; a query can hydrate from
native atoms. No index data is authoritative or persisted in the journal.
"""
from collections import deque
from dataclasses import dataclass, field


@dataclass
class DependencyIndex:
    alignments: set = field(default_factory=set)
    reviewed: set = field(default_factory=set)
    exact: set = field(default_factory=set)
    edges: set = field(default_factory=set)
    by_claim: dict = field(default_factory=dict)
    by_decl: dict = field(default_factory=dict)
    claims: dict = field(default_factory=dict)
    declarations: dict = field(default_factory=dict)
    dependencies: dict = field(default_factory=dict)

    @staticmethod
    def _adjust(mapping, source, target, delta):
        values = mapping.setdefault(source, {})
        values[target] = values.get(target, 0) + delta
        if values[target] == 0:
            del values[target]
        if not values:
            del mapping[source]

    def apply(self, parts, adding):
        kind = parts[0]
        target = None
        if kind == 'node' and parts[3] == 'Alignment':
            target, value = self.alignments, parts[2]
        elif kind == 'field' and parts[3:5] == ['lifecycle', 'reviewed']:
            target, value = self.reviewed, parts[2]
        elif kind == 'field' and parts[3:5] == ['verdict', 'aligned']:
            target, value = self.exact, parts[2]
        elif kind == 'edge' and parts[3] in {'ALIGNS_CLAIM', 'ALIGNS_DECLARATION', 'DEPENDS_ON'}:
            value = tuple(parts[2:6])
            if adding == (value in self.edges):
                return
            (self.edges.add if adding else self.edges.discard)(value)
            _, relation, source, destination = value
            delta = 1 if adding else -1
            if relation == 'DEPENDS_ON':
                self._adjust(self.dependencies, source, destination, delta)
            else:
                by_target, by_alignment = ((self.by_claim, self.claims) if relation == 'ALIGNS_CLAIM'
                                          else (self.by_decl, self.declarations))
                self._adjust(by_target, destination, source, delta)
                self._adjust(by_alignment, source, destination, delta)
        if target is not None:
            (target.add if adding else target.discard)(value)

    def closure(self, declaration_id):
        prefix, local = declaration_id.split('/', 1)
        prefix += '/'
        accepted = self.alignments & self.reviewed & self.exact
        def direct(declaration):
            matches = accepted.intersection(self.by_decl.get(declaration, ()))
            if len(matches) != 1:
                return ()
            alignment = next(iter(matches))
            source = self.claims.get(alignment, ())
            if len(source) != 1 or len(self.declarations.get(alignment, ())) != 1:
                return ()
            result = set()
            for claim in self.dependencies.get(next(iter(source)), ()):
                matches = accepted.intersection(self.by_claim.get(claim, ()))
                if len(matches) != 1:
                    return ()
                targets = self.declarations.get(next(iter(matches)), ())
                if len(targets) != 1:
                    return ()
                result.update(targets)
            return result
        seen, pending = {local}, deque([local])
        while pending:
            for target in direct(pending.popleft()):
                if target not in seen:
                    seen.add(target)
                    pending.append(target)
        seen.discard(local)
        return tuple(prefix + identity for identity in sorted(seen))


def prepare(space, command, payload):
    """Invalidate results before native mutation; retain an index only on success."""
    if command == b'restore':
        space.closure_cache.clear()
        space.dependency_indexes.clear()
        type(space).dependency_unknown = None
        return {}
    if command not in {b'add-atoms', b'remove-atoms'}:
        return {}
    from .backend.view import decode, tokens
    grouped = {}
    try:
        for atom in payload.decode('utf-8').splitlines():
            parts = tokens(atom)
            if not parts or parts[0] not in {'node', 'field', 'edge'}:
                continue
            parts = [parts[0], *(decode(token) for token in parts[1:])]
            size = {'node': 4, 'field': 5, 'edge': 6}[parts[0]]
            if (len(parts) != size or not all(isinstance(v, str) for v in parts[1:4])
                    or (parts[0] == 'edge' and not all(isinstance(v, str) for v in parts[4:6]))):
                raise ValueError('noncanonical graph atom')
            grouped.setdefault(parts[1], []).append(parts)
    except (ValueError, TypeError, IndexError):
        space.closure_cache.clear()
        space.dependency_indexes.clear()
        type(space).dependency_unknown = None
        return {}
    mutations = {}
    for proof, atoms in grouped.items():
        for key in tuple(space.closure_cache):
            if key.startswith(proof + '/'):
                del space.closure_cache[key]
        index = space.dependency_indexes.pop(proof, None)
        relevant = any((a[0] == 'node' and len(a) == 4 and a[3] == 'Alignment')
                       or (a[0] == 'field' and len(a) == 5 and a[3] in {'lifecycle', 'verdict'})
                       or (a[0] == 'edge' and len(a) == 6 and a[3] in {'ALIGNS_CLAIM', 'ALIGNS_DECLARATION', 'DEPENDS_ON'})
                       for a in atoms)
        if index is None and relevant and space.dependency_unknown is not None and proof not in space.dependency_unknown:
            index = DependencyIndex()
        mutations[proof] = (index, atoms)
    return mutations


def finish(space, changes, command, success):
    for proof, (index, atoms) in changes.items():
        if not success:
            if space.dependency_unknown is not None:
                space.dependency_unknown.add(proof)
            continue
        if index is not None:
            for atom in atoms:
                index.apply(atom, command == b'add-atoms')
            remember(space, proof, index)


def remember(space, proof, index):
    space.dependency_indexes[proof] = index
    space.dependency_indexes.move_to_end(proof)
    if space.dependency_unknown is not None:
        space.dependency_unknown.discard(proof)
    if len(space.dependency_indexes) > 16:
        evicted, _ = space.dependency_indexes.popitem(last=False)
        if space.dependency_unknown is not None:
            space.dependency_unknown.add(evicted)


def hydrate(space, proof):
    """Populate from a restored/untracked native graph; no assumptions about history."""
    from .backend.view import encode, decode, tokens
    index = DependencyIndex()
    p = encode(proof)
    for kind, pattern in [
        ('node', f'(node {p} $id "Alignment")'),
        ('field', f'(field {p} $id "lifecycle" "reviewed")'),
        ('field', f'(field {p} $id "verdict" "aligned")'),
        *[('edge', f'(edge {p} $eid "{relation}" $src $dst)')
          for relation in ('ALIGNS_CLAIM', 'ALIGNS_DECLARATION', 'DEPENDS_ON')],
    ]:
        for row in space.match(pattern, pattern):
            index.apply([kind, *(decode(token) for token in tokens(row)[1:])], True)
    remember(space, proof, index)
    return index
