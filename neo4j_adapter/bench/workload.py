"""Synthetic proof graphs and journals, generated from a seed.

A benchmark is only comparable if its input is reproducible, so everything here
takes a `random.Random` the caller seeded.

`proposals` builds gate-legal proposals for experiments that measure the commit
path. `load` commits them through a gate and projects them onto Neo4j, so the
graph holds exactly what a real run would produce.

The vocabulary matches what `validate_proposal` accepts:
    FormalState.status  open -> expanded    (needs the lease)
    depth               an annotation field (no lease, no prior)
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from commit_gate.apply import apply_ops
from commit_gate.ops import AddEdge, Op, SetField, UpsertNode
from commit_gate.proposal import Proposal

if TYPE_CHECKING:
    from commit_gate.gate import CommitGate
    from neo4j_adapter.projector import Neo4jProjector

__all__ = [
    "node_id",
    "proposals",
    "seeded",
    "load",
]

_LABEL = "FormalState"
_KINDS = ("or", "and", "goal")
_WORKER = "llm-research"


def seeded(seed: int = 20260902) -> random.Random:
    """A generator fixed for the whole run, so two runs are comparable."""
    return random.Random(seed)


def node_id(proof: str, index: int) -> str:
    """The full `<proof>/<local>` id of one generated node."""
    return f"{proof}/s{index}"


def proposals(
    proof: str, count: int, rng: random.Random, *, lease: tuple[str, int] | None = None
) -> list[Proposal]:
    """`count` proposals that the gate accepts in order, from revision 0.

    Each carries `base_revision`, since a proposal without one is rejected for a
    missing concurrency token. The mix is one node per event, an edge back into
    the graph once there is something to point at, and -- when a lease is given
    -- the status transition that makes an event look like real work.
    """
    built: list[Proposal] = []
    for revision in range(count):
        ops: list[Op] = [
            UpsertNode(
                _LABEL,
                node_id(proof, revision),
                {
                    "description": f"state {revision}",
                    "status": "open",
                    "kind": rng.choice(_KINDS),
                },
            )
        ]
        if revision:
            ops.append(
                AddEdge(
                    "CHILD_OF",
                    node_id(proof, revision),
                    node_id(proof, rng.randrange(revision)),
                    f"{proof}/e{revision}",
                )
            )
        if lease and revision:
            ops.append(
                SetField(
                    _LABEL, node_id(proof, revision - 1), "status", "expanded", "open"
                )
            )
        built.append(
            Proposal(
                proof_id=proof,
                actor="bench",
                worker_class=_WORKER,
                ops=tuple(ops),
                base_revision=revision,
                lease_id=lease[0] if lease else None,
                fencing_token=lease[1] if lease else None,
            )
        )
    return built


def load(gate: "CommitGate", projector: "Neo4jProjector", batch: list[Proposal]) -> int:
    """Commit every proposal through the gate, keep the view in sync, and project.

    The gate validates against `gate._view` but never mutates it -- `apply_ops`
    must be called after each accepted commit so subsequent proposals can
    reference the nodes that were just created.

    Returns the number of accepted commits.
    """
    accepted = 0
    for proposal in batch:
        result = gate.commit(proposal)
        if result.accepted:
            apply_ops(gate._view, list(proposal.ops))
            accepted += 1
    if batch:
        projector.catch_up(batch[0].proof_id)
    return accepted
