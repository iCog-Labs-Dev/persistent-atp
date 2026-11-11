# MORK Rules

The files in this directory define lazy, queryable MeTTa functions for the
projected proof graph. They compute derived views from atoms already present in
`&mork`; loading a module does not add atoms or mutate the graph.

## Modules and functions

### `index_views.metta`

Reverse-index views over projected edges:

- `incoming-edges P NODE` returns `(relation source edge-id)` tuples.
- `outgoing-edges P NODE` returns `(relation destination edge-id)` tuples.
- `incident-edges P NODE` combines incoming and outgoing edges.

### `frontier.metta`

Frontier and AND/OR closure queries:

- `has-field?`, `move-open?`, `move-excluded?`, and `state-blocked?` provide
  status checks.
- `is-eligible-move?` and `filter-eligible-moves` identify usable moves.
- `frontier-for-state` returns eligible moves proposed by a state.
- `excluded-moves` and `is-frontier?` expose exclusion and frontier checks.
- `state-solved?` implements OR closure: one formally closed proposed move is
  sufficient.
- `subgoal-open?`, `open-subgoal?`, and `move-complete?` implement AND closure.

### `dependency-taint.metta`

Dependency graph, cycle, and refutation-impact queries:

- `direct-depends?`, `deps-of`, and `reaches?` query direct and transitive
  `DEPENDS_ON` relationships. `reaches?` is reflexive, so a claim reaches
  itself.
- `depends-on` returns transitive dependents; `depends-on-chain` returns
  transitive dependencies.
- `would-cycle? P DEPENDENT DEPENDS-ON` checks whether adding an edge would
  create a cycle.
- `taint-cone` returns strict dependents of a refuted claim.
- `affected-states` returns formally closed states that use a strict dependent
  of a refuted claim and therefore need reopening. Non-refuted roots produce
  an empty result.

### `projections.metta`

Route and transposition-candidate queries:

- `route-steps` returns every projected edge as `(edge-id relation source
  destination)`.
- `routes-to` returns edge routes whose destination is a target node.
- `predecessors` returns incoming routes to a target node.
- `exact-duplicate-edges` finds distinct edge IDs with the same relation,
  source, and destination.
- `semantic-duplicate-claims` finds claims with the same `statement` field.
- `node-candidates` finds same-label nodes with the same `description` field.

Duplicate queries return candidates for review; they do not merge nodes or
edges automatically.

## Atom contract

Queries use projector-shaped atoms scoped by proof ID `P`, including:

```metta
(node P ID LABEL)
(field P ID NAME VALUE)
(edge P EDGE-ID RELATION SOURCE DESTINATION)
(rev-edge P NODE RELATION SOURCE EDGE-ID)
(layer P ID "committed")
```


## Typical queries

After loading a proof projection and the required module:

```metta
!(frontier-for-state "even-sum-proof" "s1")
!(would-cycle? "even-sum-proof" "c2" "c1")
!(taint-cone "even-sum-proof" "c1")
!(affected-states "even-sum-proof" "c1")
```



## Testing

The test files seed small committed graphs in `&mork` and test each module in
isolation. From `mork/rules/tests/`, run any individual test with the PeTTa
runner:

```sh
petta test_index_views.metta
petta test_frontier.metta
petta test_dependency_taint.metta
petta test_projections.metta
```
