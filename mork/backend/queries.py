"""The 15 domain query operations (spec section 18) as `MorkView` methods.

`MorkView` mixes this in; the methods compose `nodes`/`edges_from`/
`edges_to` plus Python traversal for transitive closure, per the `.mm2`
deviation record (Python `MorkView` is the only live derived-query engine
for the PoC). All relation names and labels are the gate's canonical
vocabulary (`src/shared/vocab.py`, `commit_gate.validate.EDGE_ENDPOINTS`).

Conventions:
- Accepted alignment: ``lifecycle == "reviewed"`` and
  ``verdict == "aligned"``. Anything else is reported through the
  diagnostic field, never silently resolved.
- Run membership (FORMAL_RUN as a derived index): a tactic attributes its
  run with ``tactic -RUN_OF-> run``; member states of a run are the source
  states of its tactics (HAS_TACTIC), the child obligations those tactics
  produced (FORMAL_REQUIRES), and the run's roots (HAS_ROOT).
- Certificate nodes carry the adapter's seven-field ``witness`` record.
  Its state→tactic selection belongs to that certificate, and ordered child
  obligations are checked against ``child_index`` edge fields. Shared legacy
  ``selected`` flags are never used to infer a current witness.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from commit_gate.state import EdgeRecord, NodeRecord

if TYPE_CHECKING:
    from mork.backend.view import MorkView

ACCEPTED_ALIGNMENT = {"lifecycle": "reviewed", "verdict": "aligned"}
ENVIRONMENT_RELATIONS = ("PINNED_ENVIRONMENT", "RAN_UNDER", "CERTIFICATE_ENVIRONMENT", "REPLAY_ENVIRONMENT")


@dataclass(frozen=True)
class DependencyResult:
    """Direct dependencies of one declaration, with the §7.1 diagnostic."""

    declarations: tuple[str, ...]
    diagnostic: str | None = None


@dataclass(frozen=True)
class EvidenceChain:
    """The claim -> ... -> replay evidence chain (spec section 10)."""

    claim: NodeRecord | None
    alignment: NodeRecord | None
    declaration: NodeRecord | None
    runs: tuple[NodeRecord, ...]
    certificates: tuple[NodeRecord, ...]
    replays: tuple[NodeRecord, ...]
    consistent: bool


@dataclass(frozen=True)
class WitnessState:
    """One node of the certificate witness DAG."""

    state: NodeRecord
    tactic: EdgeRecord | None  # the selected tactic edge that produced it
    children: tuple["WitnessState", ...] = field(default_factory=tuple)
    closure: bool = False  # zero-goal closure, executor_result lean-accepted
    selected_tactic: EdgeRecord | None = None  # this state's certificate-scoped selection


def _accepted_alignment(self: MorkView, edges: tuple[EdgeRecord, ...]) -> tuple[NodeRecord | None, str | None]:
    """Resolve the single accepted alignment among reverse ALIGNS_CLAIM edges.

    Missing / ambiguous / stale / non-exact all return (None, diagnostic) —
    never a silently picked first (§7.1).
    """
    if not edges:
        return None, "missing-alignment"
    alignments = [self.node(edge.src_id) for edge in edges]
    alignments = [a for a in alignments if a is not None]
    accepted = [
        a
        for a in alignments
        if all(a.fields.get(k) == v for k, v in ACCEPTED_ALIGNMENT.items())
    ]
    if len(accepted) > 1:
        return None, "ambiguous-alignment"
    if len(accepted) == 1:
        alignment = accepted[0]
        claims = self.edges_from(alignment.node_id, "ALIGNS_CLAIM")
        declarations = self.edges_from(alignment.node_id, "ALIGNS_DECLARATION")
        if len(claims) != 1:
            return None, "ambiguous-claim" if claims else "missing-claim"
        if len(declarations) != 1:
            return None, "ambiguous-declaration" if declarations else "missing-declaration"
        return alignment, None
    if any(a.fields.get("lifecycle") in ("superseded", "stale") for a in alignments):
        return None, "stale-alignment"
    return None, "non-exact-alignment"


class QueryOps:
    """Mixed into MorkView; see module docstring for conventions."""

    # ---------------------------------------------------------------- root

    def get_claim(self: MorkView, proof_id: str) -> NodeRecord | None:
        """The root mathematical claim: Proof -HAS_TARGET-> Claim."""
        proofs = self.nodes(proof_id, "Proof")
        if not proofs:
            return None
        targets = self.edges_from(proofs[0].node_id, "HAS_TARGET")
        if not targets:
            return None
        return self.node(targets[0].dst_id)

    def get_alignment(self: MorkView, claim_id: str) -> NodeRecord | None:
        alignment, _ = _accepted_alignment(self, self.edges_to(claim_id, "ALIGNS_CLAIM"))
        return alignment

    def get_declaration(self: MorkView, claim_id: str) -> NodeRecord | None:
        """Resolve the Lean declaration through the accepted alignment."""
        alignment, _ = _accepted_alignment(self, self.edges_to(claim_id, "ALIGNS_CLAIM"))
        if alignment is None:
            return None
        declarations = self.edges_from(alignment.node_id, "ALIGNS_DECLARATION")
        if not declarations:
            return None
        return self.node(declarations[0].dst_id)

    # ----------------------------------------------------------------- run

    def get_run(self: MorkView, run_id: str) -> dict[str, Any]:
        run = self.node(run_id)
        return {
            "run": run,
            "checkpoints": tuple(
                self.node(edge.dst_id)
                for edge in self.edges_from(run_id, "HAS_CHECKPOINT")
            ),
            "roots": tuple(
                self.node(edge.dst_id) for edge in self.edges_from(run_id, "HAS_ROOT")
            ),
        }

    def get_run_states(self: MorkView, run_id: str) -> tuple[NodeRecord, ...]:
        """States explored by the run — the derived FORMAL_RUN membership set."""
        member_ids: set[str] = set()
        for root in self.edges_from(run_id, "HAS_ROOT"):
            member_ids.add(root.dst_id)
        for edge in self.edges_to(run_id, "RUN_OF"):
            tactic_id = edge.src_id
            for incoming in self.edges_to(tactic_id, "HAS_TACTIC"):
                member_ids.add(incoming.src_id)
            for child in self.edges_from(tactic_id, "FORMAL_REQUIRES"):
                member_ids.add(child.dst_id)
        return tuple(
            sorted(
                (record for node_id in member_ids if (record := self.node(node_id)) is not None),
                key=lambda record: record.node_id,
            )
        )

    # -------------------------------------------------------------- tactics

    def get_outgoing_tactics(self: MorkView, state_id: str) -> tuple[EdgeRecord, ...]:
        return self.edges_from(state_id, "HAS_TACTIC")

    def get_tactic_children(self: MorkView, tactic_id: str) -> tuple[EdgeRecord, ...]:
        """Ordered child obligations (sorted by the child_index efield)."""
        return tuple(
            sorted(
                self.edges_from(tactic_id, "FORMAL_REQUIRES"),
                key=lambda edge: edge.fields.get("child_index", 0),
            )
        )

    def get_obstructions(self: MorkView, state_id: str) -> tuple[NodeRecord, ...]:
        """Structured failures at the exact state (Obstruction -AT_STATE->)."""
        return tuple(
            record
            for edge in self.edges_to(state_id, "AT_STATE")
            if (record := self.node(edge.src_id)) is not None
        )

    # ---------------------------------------------------------- dependencies

    def get_dependencies(self: MorkView, declaration_id: str) -> DependencyResult:
        """Direct dependencies per §7.1: declaration -> alignment -> claim ->
        DEPENDS_ON (Claim -> Claim) -> the dependent claims' declarations."""
        alignment, diagnostic = _accepted_alignment(
            self, self.edges_to(declaration_id, "ALIGNS_DECLARATION")
        )
        if alignment is None:
            return DependencyResult((), diagnostic)
        claims = self.edges_from(alignment.node_id, "ALIGNS_CLAIM")
        if not claims:
            return DependencyResult((), "missing-claim")
        claim_id = claims[0].dst_id
        declarations: set[str] = set()
        for dep in self.edges_from(claim_id, "DEPENDS_ON"):
            dep_alignment, diag = _accepted_alignment(
                self, self.edges_to(dep.dst_id, "ALIGNS_CLAIM")
            )
            if dep_alignment is None:
                return DependencyResult((), f"dependency-{diag}")
            for out in self.edges_from(dep_alignment.node_id, "ALIGNS_DECLARATION"):
                declarations.add(out.dst_id)
        return DependencyResult(tuple(sorted(declarations)), None)

    def get_transitive_dependencies(self: MorkView, declaration_id: str) -> tuple[str, ...]:
        """Cycle-safe closure over a bulk-read, accepted-alignment index.

        The Python index follows canonical mutations across wrappers. Unknown
        state is hydrated from native atoms; results are invalidated per proof.
        The complete query holds the shared lock.
        """
        cache = self._space.closure_cache
        if declaration_id in cache:
            cache.move_to_end(declaration_id)
            return cache[declaration_id]
        from mork.dependencies import hydrate
        proof = declaration_id.split("/", 1)[0]
        index = self._space.dependency_indexes.get(proof)
        if index is None:
            index = hydrate(self._space, proof)
        result = index.closure(declaration_id)
        cache[declaration_id] = result
        if len(cache) > 16:
            cache.popitem(last=False)
        return result

    # ------------------------------------------------------------ certificates

    def get_pending_replay(self: MorkView, proof_id: str) -> dict[str, tuple[NodeRecord, ...]]:
        """Candidate vs replay-pending certificates, kept distinct."""
        candidates: list[NodeRecord] = []
        pending: list[NodeRecord] = []
        for record in self.nodes(proof_id, "Certificate"):
            status = record.fields.get("status")
            if status == "candidate":
                candidates.append(record)
            elif status == "replay-pending":
                pending.append(record)
        return {
            "candidates": tuple(candidates),
            "replay_pending": tuple(pending),
        }

    def get_exact_state(self: MorkView, state_id: str, environment_hash: str | None = None) -> NodeRecord | None:
        """Current exact reuse only; unversioned history stays available via node()."""
        from mathproof.state_identity import identity_issue
        state = self.node(state_id)
        if state is None or state.label != "FormalState" or identity_issue(state.fields, environment_hash):
            return None
        return state

    def get_certificate_witness(self: MorkView, cert_id: str) -> WitnessState | None:
        """Read only the certificate's explicit upstream selection; never infer it."""
        from mathproof.witness import validate_witness
        certificate = self.node(cert_id)
        if certificate is None or certificate.label != "Certificate" or "witness" not in certificate.fields:
            return None  # historical certificate, no current witness contract
        record = certificate.fields["witness"]
        if not isinstance(record, dict) or not isinstance(record.get("selected_tactics"), dict):
            raise ValueError("malformed certificate witness selection")
        if certificate.fields.get("root_state_id", record.get("root_state_id")) != record.get("root_state_id"):
            raise ValueError("certificate root disagrees with witness")
        states, tactics, selected_edges = {}, [], {}
        for state_id, tactic_id in record["selected_tactics"].items():
            state = self.node(state_id)
            tactic = self.node(tactic_id)
            edge = [e for e in self.get_outgoing_tactics(state_id) if e.dst_id == tactic_id]
            if (state is None or state.label != "FormalState" or tactic is None
                    or tactic.label != "TacticApplication" or len(edge) != 1):
                raise ValueError("missing or ambiguous witness state/tactic edge")
            states[state_id] = state
            selected_edges[state_id] = edge[0]
            children = self.get_tactic_children(tactic_id)
            if (any(type(child.fields.get("child_index")) is not int for child in children)
                    or [child.fields.get("child_index") for child in children] != list(range(len(children)))):
                raise ValueError("invalid witness child indices")
            tactics.append({**tactic.fields, "tactic_id": tactic_id, "source_state_id": state_id,
                            "produced_goal_ids": [child.dst_id for child in children]})
        result = {"proof_id": cert_id.split("/", 1)[0], "root_state_id": record.get("root_state_id"),
                  "certificate": certificate.fields, "states": [{"state_id": identity} for identity in states],
                  "tactic_edges": tactics}
        validate_witness(record, result, certificate_id=cert_id)
        by_source = {edge["source_state_id"]: edge for edge in tactics}
        def build(state_id, via=None):
            children = tuple(build(child, selected_edges[state_id])
                             for child in by_source[state_id]["produced_goal_ids"])
            return WitnessState(states[state_id], via, children, not children, selected_edges[state_id])
        return build(record["root_state_id"])

    def get_claim_evidence(self: MorkView, claim_id: str) -> EvidenceChain:
        """The complete verification chain plus the consistency audit.

        ``consistent`` is the derived alignment-evidence-consistent audit
        relation: true only when every environment identity present across
        the chain (declaration pinning, run, replay) is the same. A drift
        never derives consistency (spec section 10).
        """
        alignment, _ = _accepted_alignment(self, self.edges_to(claim_id, "ALIGNS_CLAIM"))
        declaration = None
        if alignment is not None:
            decl_edges = self.edges_from(alignment.node_id, "ALIGNS_DECLARATION")
            if decl_edges:
                declaration = self.node(decl_edges[0].dst_id)

        runs: list[NodeRecord] = []
        certificates: list[NodeRecord] = []
        replays: list[NodeRecord] = []
        if declaration is not None:
            for edge in self.edges_to(declaration.node_id, "SEARCHES"):
                if (run := self.node(edge.src_id)) is not None:
                    runs.append(run)
                for cert_edge in self.edges_from(edge.src_id, "PRODUCED_CERTIFICATE"):
                    if (cert := self.node(cert_edge.dst_id)) is not None:
                        certificates.append(cert)
                    for replay_edge in self.edges_from(cert_edge.dst_id, "REPLAYED_BY"):
                        if (replay := self.node(replay_edge.dst_id)) is not None:
                            replays.append(replay)

        environments: set[str] = set()
        hashes: set[str] = set()
        complete_bindings = True
        for node in [declaration, *runs, *certificates, *replays]:
            if node is None:
                complete_bindings = False
                continue
            bound = set()
            for relation in ENVIRONMENT_RELATIONS:
                for edge in self.edges_from(node.node_id, relation):
                    bound.add(edge.dst_id)
            complete_bindings = complete_bindings and len(bound) == 1
            environments.update(bound)
            if node.fields.get("environment_hash"):
                hashes.add(node.fields["environment_hash"])
            for environment_id in bound:
                environment = self.node(environment_id)
                if environment is None:
                    complete_bindings = False
                elif environment.fields.get("environment_hash"):
                    hashes.add(environment.fields["environment_hash"])
        consistent = bool(alignment and declaration and runs and certificates and replays) and (
            complete_bindings and len(environments) == 1 and len(hashes) <= 1
        )

        return EvidenceChain(
            claim=self.node(claim_id),
            alignment=alignment,
            declaration=declaration,
            runs=tuple(runs),
            certificates=tuple(certificates),
            replays=tuple(replays),
            consistent=consistent,
        )

    # ------------------------------------------------------------ similarity

    def get_semantic_peers(self: MorkView, state_id: str) -> tuple[NodeRecord, ...]:
        """States with the same semantic signature and a different exact id.

        Similarity only ever proposes peers; it never merges exact states.
        """
        state = self.node(state_id)
        if state is None:
            return ()
        signature = state.fields.get("semantic_signature")
        exact = state.fields.get("exact_state_hash")
        if signature is None:
            return ()
        proof_id = state_id.split("/", 1)[0]
        return tuple(
            record
            for record in self.nodes(proof_id, "FormalState")
            if record.node_id != state_id
            and record.fields.get("semantic_signature") == signature
            and record.fields.get("exact_state_hash") != exact
        )

    def get_dependents(self: MorkView, claim_id: str) -> tuple[str, ...]:
        """Cycle-safe reverse dependency impact; never change proof status."""
        visited = {claim_id}
        pending = deque([claim_id])
        while pending:
            for edge in self.edges_to(pending.popleft(), "DEPENDS_ON"):
                if edge.src_id not in visited:
                    visited.add(edge.src_id)
                    pending.append(edge.src_id)
        return tuple(sorted(visited - {claim_id}))

    def get_stale_impact(self: MorkView, node_id: str) -> tuple[str, ...]:
        """Find affected claims, declarations, runs and certificates for audit.

        This is structural impact, not authority to mark anything stale.
        """
        visited = {node_id}
        pending = deque([node_id])
        reverse = ("DEPENDS_ON", "ALIGNS_CLAIM", "ALIGNS_DECLARATION",
                   "PINNED_ENVIRONMENT", "CERTIFICATE_ENVIRONMENT", "REPLAY_ENVIRONMENT",
                   "RAN_UNDER", "PROVED_BY", "CERTIFIES", "SEARCHES", "REPLAYED_BY")
        forward = ("ALIGNS_CLAIM", "ALIGNS_DECLARATION", "PRODUCED_CERTIFICATE",
                   "REPLAYED_BY", "CERTIFIES", "HAS_ROOT", "HAS_CHECKPOINT",
                   "HAS_TACTIC", "FORMAL_REQUIRES")
        while pending:
            current = pending.popleft()
            adjacent = {e.src_id for relation in reverse for e in self.edges_to(current, relation)}
            adjacent.update(e.dst_id for relation in forward for e in self.edges_from(current, relation))
            for identity in adjacent - visited:
                visited.add(identity)
                pending.append(identity)
        return tuple(sorted(visited - {node_id}))

    def get_discovery(self: MorkView, proof_id: str) -> dict[str, tuple[NodeRecord, ...]]:
        """Scheduler discovery without scoring or status changes."""
        return {
            "open_states": tuple(n for n in self.nodes(proof_id, "FormalState")
                                 if n.fields.get("status") == "open"),
            "obstructions": tuple(n for n in self.nodes(proof_id, "Obstruction")
                                  if not self.edges_to(n.node_id, "RESOLVES")),
            "research_moves": self.nodes(proof_id, "ResearchMove"),
            "alignments": self.nodes(proof_id, "Alignment"),
        }

    # ------------------------------------------------------------- watermark

    def get_projector_watermark(self: MorkView, proof_id: str) -> dict[str, int]:
        return {
            "revision": self.projected_revision(proof_id),
            "schema_version": self.projected_schema_version(proof_id),
        }
