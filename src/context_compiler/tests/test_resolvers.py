import pytest

from commit_gate.state import MemoryView
from context_compiler.errors import (
    ContextValidationError,
    FormalRunPreparationRequired,
)
from context_compiler.resolvers import resolve_formal_task, resolve_research_task
from context_compiler.snapshot import ProofSnapshot


def research_snapshot(*, move_fields=None, second_parent=False, move_target=True):
    view = MemoryView()
    view.add_node(
        "p1/c-1",
        "Claim",
        {"status": "conjectural", "statement": "Every even square is even."},
    )
    view.add_node(
        "p1/c-2",
        "Claim",
        {"status": "critic-accepted", "statement": "The parity lemma."},
    )
    view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
    fields = {
        "status": "queued",
        "description": "Try induction.",
        "required_claim_ids": ["p1/c-2"],
    }
    fields.update(move_fields or {})
    view.add_node("p1/rm-1", "ResearchMove", fields)
    view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/e-1")
    if second_parent:
        view.add_node("p1/rs-2", "ResearchState", {"status": "open"})
        view.add_edge("PROPOSES", "p1/rs-2", "p1/rm-1", "p1/e-2")
    if move_target:
        view.add_edge("MOVE_TARGETS", "p1/rm-1", "p1/c-1", "p1/e-3")
    return ProofSnapshot("p1", 4, view)


def test_resolver_uses_graph_relationships_and_includes_required_claim_seeds():
    resolved = resolve_research_task(research_snapshot(), "p1/rm-1")

    assert resolved.kernel_id == "p1/c-1"
    assert resolved.active_state_id == "p1/rs-1"
    assert resolved.selected_move_id == "p1/rm-1"
    assert resolved.required_claim_ids == ("p1/c-2",)
    assert resolved.seed_ids == ("p1/rs-1", "p1/rm-1", "p1/c-2")


def test_resolver_falls_back_to_the_proof_target():
    snapshot = research_snapshot(move_target=False)
    view = snapshot.view
    view.add_node("p1/proof", "Proof", {"status": "open"})
    view.add_edge("HAS_TARGET", "p1/proof", "p1/c-1", "p1/e-target")

    resolved = resolve_research_task(snapshot, "p1/rm-1")

    assert resolved.kernel_id == "p1/c-1"


def test_resolver_accepts_an_explicit_kernel():
    resolved = resolve_research_task(
        research_snapshot(move_target=False),
        "p1/rm-1",
        kernel_id="p1/c-1",
    )

    assert resolved.kernel_id == "p1/c-1"


def test_resolver_rejects_ambiguous_parent_state():
    with pytest.raises(ContextValidationError, match="exactly one PROPOSES parent"):
        resolve_research_task(
            research_snapshot(second_parent=True), "p1/rm-1"
        )


def test_resolver_rejects_state_field_which_disagrees_with_the_edge():
    with pytest.raises(ContextValidationError, match="disagrees with PROPOSES"):
        resolve_research_task(
            research_snapshot(move_fields={"state_id": "p1/rs-99"}),
            "p1/rm-1",
        )


def test_resolver_rejects_a_dead_parent_state():
    snapshot = research_snapshot()
    snapshot.view.set_field("p1/rs-1", "status", "superseded")

    with pytest.raises(ContextValidationError, match="dead status"):
        resolve_research_task(snapshot, "p1/rm-1")


def test_resolver_rejects_a_parent_without_a_valid_status():
    snapshot = research_snapshot()
    snapshot.view.set_field("p1/rs-1", "status", "mystery")

    with pytest.raises(ContextValidationError, match="invalid status"):
        resolve_research_task(snapshot, "p1/rm-1")


def test_resolver_rejects_a_required_claim_from_another_proof():
    with pytest.raises(ContextValidationError, match="does not belong"):
        resolve_research_task(
            research_snapshot(
                move_fields={"required_claim_ids": ["p2/c-1"]}
            ),
            "p1/rm-1",
        )


def test_resolver_requires_one_kernel_when_no_override_or_target_exists():
    with pytest.raises(ContextValidationError, match="exactly one theorem kernel"):
        resolve_research_task(
            research_snapshot(move_target=False), "p1/rm-1"
        )


def formal_snapshot(*, selected="p1/fr-1", second_root=False, pin="p1/env-1"):
    view = MemoryView()
    view.add_node(
        "p1/c-1",
        "Claim",
        {"status": "provisional", "statement": "For every n, P n."},
    )
    view.add_node(
        "p1/fd-1",
        "FormalDeclaration",
        {
            "status": "searching",
            "claim_id": "p1/c-1",
            "lean_type": "∀ n, P n",
        },
    )
    view.add_node("p1/fr-1", "FormalRun", {"status": "searching"})
    view.add_node("p1/fs-1", "FormalState", {"status": "open"})
    view.add_node("p1/env-1", "Environment", {"toolchain": "lean4"})
    view.add_node("p1/env-2", "Environment", {"toolchain": "lean4"})
    view.add_node(
        "p1/fd-2",
        "FormalDeclaration",
        {"status": "replay-accepted", "lean_type": "P 0"},
    )
    view.add_edge("SEARCHES", "p1/fr-1", "p1/fd-1", "p1/e-search")
    view.add_edge("HAS_ROOT", "p1/fr-1", "p1/fs-1", "p1/e-root")
    if second_root:
        view.add_node("p1/fs-2", "FormalState", {"status": "open"})
        view.add_edge("HAS_ROOT", "p1/fr-1", "p1/fs-2", "p1/e-root-2")
    view.add_edge("RAN_UNDER", "p1/fr-1", "p1/env-1", "p1/e-env")
    view.add_edge("PINNED_ENVIRONMENT", "p1/fd-1", pin, "p1/e-pin")
    return ProofSnapshot("p1", 4, view), selected


def test_formal_resolver_resolves_existing_run_and_explicit_premises():
    snapshot, selected = formal_snapshot()

    resolved = resolve_formal_task(
        snapshot, selected, premise_ids=("p1/fd-2",)
    )

    assert resolved.kernel_id == "p1/c-1"
    assert resolved.claim_id == "p1/c-1"
    assert resolved.declaration_id == "p1/fd-1"
    assert resolved.run_id == "p1/fr-1"
    assert resolved.root_state_id == "p1/fs-1"
    assert resolved.environment_id == "p1/env-1"
    assert resolved.premise_ids == ("p1/fd-2",)
    assert resolved.seed_ids == ("p1/fr-1", "p1/fd-2")
    assert resolved.is_resume is True


def test_formal_resolver_reports_declaration_preparation_requirement():
    snapshot, _ = formal_snapshot(selected="p1/fd-1")

    with pytest.raises(FormalRunPreparationRequired) as raised:
        resolve_formal_task(snapshot, "p1/fd-1")

    assert raised.value.declaration_id == "p1/fd-1"


def test_formal_resolver_rejects_ambiguous_root_state():
    snapshot, selected = formal_snapshot(second_root=True)

    with pytest.raises(ContextValidationError, match="exactly one HAS_ROOT"):
        resolve_formal_task(snapshot, selected)


def test_formal_resolver_rejects_environment_pin_mismatch():
    snapshot, selected = formal_snapshot(pin="p1/env-2")

    with pytest.raises(ContextValidationError, match="environment disagrees"):
        resolve_formal_task(snapshot, selected)


def test_formal_resolver_rejects_cross_proof_premise():
    snapshot, selected = formal_snapshot()

    with pytest.raises(ContextValidationError, match="does not belong"):
        resolve_formal_task(snapshot, selected, premise_ids=("p2/fd-1",))
