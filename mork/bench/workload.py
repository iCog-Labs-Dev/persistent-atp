"""Synthetic proof graphs and journals, generated from a seed.

A benchmark is only comparable if its input is reproducible, so everything here
takes a `random.Random` the caller seeded.

Two shapes are produced. `proof_atoms` and `background_atoms` write
s-expressions straight into a space, which is how bulk load is built: they skip
the gate and the view entirely, so reaching a given space size costs a few FFI
crossings rather than a few thousand. `proposals` builds gate-legal proposals,
for the experiments that measure the commit path.

The vocabulary is not decorative. `validate_proposal` rejects a label outside
`GATE_LABELS`, a status value outside its enum, a status transition that is not
allowed, and a status-class write with no lease. What is generated here is what
the gate actually accepts:

    FormalState.status  open -> expanded    (needs the lease)
    depth               an annotation field (no lease, no prior)
"""

from __future__ import annotations

import random
from typing import Iterable, Iterator

from commit_gate.ops import AddEdge, Op, SetField, UpsertNode
from commit_gate.proposal import Proposal
from mork.backend import MorkSpace
from mork.backend.view import encode

__all__ = [
    "background_atoms",
    "load",
    "node_id",
    "proof_atoms",
    "proposals",
    "seeded",
]

_LABEL = "FormalState"
_KINDS = ("or", "and", "goal")
_WORKER = "llm-research"
_BATCH = 2000  # atoms per `add`, so bulk load costs one crossing per 2,000


def seeded(seed: int = 20260902) -> random.Random:
    """A generator fixed for the whole run, so two runs are comparable."""
    return random.Random(seed)


def node_id(proof: str, index: int) -> str:
    """The full `<proof>/<local>` id of one generated node."""
    return f"{proof}/s{index}"


def proof_atoms(
    proof: str, nodes: int, rng: random.Random, *, start: int = 0
) -> Iterator[str]:
    """Atoms for nodes `start`..`nodes` of one proof: nodes, fields, and edges.

    Written directly rather than through `MorkView`, because the point is to
    reach a given space size cheaply. The shapes match `MorkView`'s exactly --
    ids local, every slot JSON -- so what is measured afterwards is measured
    against atoms indistinguishable from committed ones.

    `start` lets a proof grow in place: an experiment that sweeps 100, 500, 2500
    nodes adds only the new ones each round instead of rebuilding the whole
    proof. Each node costs four atoms plus, for every node but the first, one
    edge back into what is already there.
    """
    p = encode(proof)
    for index in range(start, nodes):
        local = encode(f"s{index}")
        yield f"(node {p} {local} {encode(_LABEL)})"
        yield f"(field {p} {local} {encode('description')} {encode(f'state {index}')})"
        yield f"(field {p} {local} {encode('status')} {encode('open')})"
        yield f"(field {p} {local} {encode('kind')} {encode(rng.choice(_KINDS))})"
        if index:
            parent = encode(f"s{rng.randrange(index)}")
            edge = encode(f"e{index}")
            yield f"(edge {p} {edge} {encode('CHILD_OF')} {local} {parent})"


def background_atoms(
    proofs: int, nodes_each: int, rng: random.Random, *, first: int = 0
) -> Iterator[str]:
    """Atoms spread over `proofs` unrelated proofs, named `bg<first>` onward.

    The load E1 puts in the space to see whether one proof's queries notice
    other proofs. `first` continues where an earlier call stopped, so successive
    rounds add genuinely new proofs; reusing names would re-add atoms the space
    already holds and the space would grow by less than asked.
    """
    for index in range(first, first + proofs):
        yield from proof_atoms(f"bg{index}", nodes_each, rng)


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


def load(space: MorkSpace, sexprs: Iterable[str]) -> None:
    """Add every atom in `sexprs` to the space, one FFI crossing per batch.

    `MorkSpace.add` joins its arguments with newlines into a single payload, so a
    batch costs one crossing. Batched rather than unbounded because that payload
    is built as one string in memory first.
    """
    pending: list[str] = []
    for sexpr in sexprs:
        pending.append(sexpr)
        if len(pending) >= _BATCH:
            space.add(*pending)
            pending.clear()
    if pending:
        space.add(*pending)
