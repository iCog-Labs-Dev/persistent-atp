"""Tests for the formal obstruction packet compiler."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from commit_gate.canon import content_hash
from context_compiler.contracts import (
    CompileRequest,
    PacketKind,
    SourceBundle,
    SourceRef,
    TextBudget,
    TheoremKernel,
)
from context_compiler.errors import ContextValidationError
from context_compiler.obstruction import (
    CharHeuristicTokenCounter,
    MappingArtifactReader,
    ObstructionTask,
    SuspectedCause,
    compile_obstruction,
)
from shared.vocab import (
    ClaimStatus,
    EvidenceKind,
    ExecutorResult,
    FormalStateStatus,
    ObstructionKind,
    WorkerClass,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "obstruction.schema.json"

VERSION = "revision-12"
ENV_HASH = "sha256:" + "c3" * 32
DIAG_1 = "sha256:" + "73" * 32
DIAG_2 = "sha256:" + "74" * 32
SEM_10 = "sha256:" + "6a" * 32
KERNEL = TheoremKernel(
    kernel_id="p1/c-1",
    statement="For every n, the sum of the first n natural numbers is n(n+1)/2.",
    assumptions=("n is a natural number", "the proof is constructive"),
    status=ClaimStatus.PROVISIONAL,
)

HYP_10 = "IH : \u03a3 k=1..n k = n(n+1)/2"
TARGET_10 = (
    "inductive step: \u03a3 k=1..(n+1) = ((n+1)(n+2))/2 given IH"
)
HYP_11 = "h : P \u2227 Q"
TARGET_11 = "\u2192 direction: P \u2227 Q \u22a2 Q \u2227 P, component 1 (Q)"
TARGET_12 = "successor case: n + m = m + n"
TARGET_13 = "base case: n + 0 = n"
TARGET_14 = "rephrased inductive step with the same semantics"


def _state(
    state_id: str,
    *,
    hypotheses: tuple[str, ...],
    target: str,
    status: str = "open",
    semantic: str | None = None,
) -> dict:
    body = {
        "state_id": state_id,
        "hypotheses": list(hypotheses),
        "target": target,
        "status": status,
        "exact_state_hash": content_hash(["exact", state_id]),
    }
    if semantic is not None:
        body["semantic_signature"] = semantic
    return body


def _attempt(
    attempt_id: str,
    state_id: str,
    *,
    family: str,
    premises: tuple[str, ...] = (),
    result: str = "lean-rejected",
    diagnostic: str | None = None,
    failure_family: str | None = None,
) -> dict:
    body = {
        "attempt_id": attempt_id,
        "state_id": state_id,
        "tactic_family": family,
        "premises": list(premises),
        "executor_result": result,
    }
    if diagnostic is not None:
        body["diagnostic"] = diagnostic
    if failure_family is not None:
        body["failure_family"] = failure_family
    return body


def _diag(artifact_hash: str, text: str, *, family: str, occurrences: int) -> dict:
    return {
        "artifact_hash": artifact_hash,
        "text": text,
        "failure_family": family,
        "occurrences": occurrences,
    }


def _obstruction() -> dict:
    return {
        "obstruction_id": "p1/obs-2",
        "kind": "library-gap",
        "formal_run_id": "p1/fr-4",
        "formal_state_ids": ["p1/fs-10"],
        "diagnostic_artifacts": [DIAG_1],
        "repeated_failure_family": "unknown-identifier",
        "premises_considered": ["Finset.sum_range_succ", "Nat.sum_range_add"],
        "tactic_families_considered": ["rw", "simp", "induction"],
        "evidence": [
            {
                "kind": "heuristic-optimizer",
                "note": "same failure family across 12 tactic attempts",
            }
        ],
        "suggested_escalation": "premise-expansion",
        "environment_hash": ENV_HASH,
    }


def _run() -> dict:
    return {
        "run_id": "p1/fr-4",
        "disposition": "stagnated",
        "environment_hash": ENV_HASH,
        "frontier_state_ids": ["p1/fs-10"],
    }


def default_entries() -> list[tuple[str, str, dict]]:
    return [
        ("formal-state", "p1/fs-10", _state(
            "p1/fs-10", hypotheses=(HYP_10,), target=TARGET_10, semantic=SEM_10
        )),
        ("formal-state", "p1/fs-11", _state(
            "p1/fs-11", hypotheses=(HYP_11,), target=TARGET_11
        )),
        ("formal-state", "p1/fs-12", _state(
            "p1/fs-12", hypotheses=(), target=TARGET_12
        )),
        ("formal-state", "p1/fs-13", _state(
            "p1/fs-13", hypotheses=(), target=TARGET_13, status="tainted"
        )),
        ("formal-state", "p1/fs-14", _state(
            "p1/fs-14", hypotheses=(HYP_10,), target=TARGET_14, semantic=SEM_10
        )),
        ("attempt", "p1/ta-6", _attempt(
            "p1/ta-6",
            "p1/fs-10",
            family="rw",
            premises=("Finset.sum_range_succ", "Nat.sum_range_add"),
            diagnostic=DIAG_1,
            failure_family="unknown-identifier",
        )),
        ("attempt", "p1/ta-7", _attempt(
            "p1/ta-7", "p1/fs-11", family="simp", result="timeout"
        )),
        ("attempt", "p1/ta-8", _attempt(
            "p1/ta-8", "p1/fs-12", family="induction", premises=("Nat.rec",)
        )),
        ("attempt", "p1/ta-9", _attempt(
            "p1/ta-9", "p1/fs-13", family="simp", diagnostic=DIAG_2
        )),
        ("attempt", "p1/ta-10", _attempt(
            "p1/ta-10", "p1/fs-14", family="rw"
        )),
        ("attempt", "p1/ta-11", _attempt(
            "p1/ta-11", "p1/fs-14", family="simp"
        )),
        ("diagnostic", "p1/dg-1", _diag(
            DIAG_1,
            "unknown identifier: Finset.sum_range_succ",
            family="unknown-identifier",
            occurrences=12,
        )),
        ("diagnostic", "p1/dg-2", _diag(
            DIAG_2,
            "type mismatch at n + 0 = n",
            family="type-error",
            occurrences=3,
        )),
        ("obstruction", "p1/obs-2", _obstruction()),
        ("formal-run", "p1/fr-4", _run()),
    ]


def make_bundle(entries: list[tuple[str, str, dict]]):
    refs: list[SourceRef] = []
    artifacts: dict[str, dict] = {}
    for source_type, source_id, body in entries:
        if source_type == "diagnostic":
            content = body["artifact_hash"]
        else:
            content = content_hash(body)
        refs.append(SourceRef(source_type, source_id, VERSION, content))
        artifacts[source_id] = body
    bundle = SourceBundle("p1/bundle-obs", tuple(refs))
    return bundle, artifacts


def make_request(**overrides) -> CompileRequest:
    values = {
        "task_id": "task-7",
        "proof_id": "p1",
        "base_revision": 12,
        "packet_kind": PacketKind.OBSTRUCTION,
        "worker_class": WorkerClass.COORDINATOR,
        "text_budget": TextBudget(1200),
    }
    values.update(overrides)
    return CompileRequest(**values)


def make_task() -> ObstructionTask:
    return ObstructionTask(KERNEL, VERSION)


def scenario(**overrides) -> SimpleNamespace:
    bundle, artifacts = make_bundle(default_entries())
    return SimpleNamespace(
        request=make_request(**overrides),
        bundle=bundle,
        reader=MappingArtifactReader(artifacts),
        task=make_task(),
    )


def compile_default(**overrides) -> object:
    case = scenario(**overrides)
    return compile_obstruction(case.request, case.bundle, case.reader, case.task)


def test_compiles_payload_packet_and_manifest():
    compilation = compile_default()
    payload = compilation.payload

    assert payload.kind is ObstructionKind.LIBRARY_GAP
    assert payload.obstruction_id == "p1/obs-2"
    assert payload.formal_run_id == "p1/fr-4"
    assert payload.environment_hash == ENV_HASH
    assert payload.suggested_escalation == "premise-expansion"
    assert payload.repeated_failure_family == "unknown-identifier"

    assert compilation.packet.packet_kind is PacketKind.OBSTRUCTION
    assert compilation.manifest["packet_digest"] == compilation.packet_digest
    assert compilation.manifest["budgets"] == {"text_tokens": 1200}
    assert compilation.manifest["compiler_version"] == "context/1"
    assert compilation.manifest["rendering_version"] == "text/1"
    assert compilation.packet_digest.startswith("sha256:")


def test_theorem_kernel_is_rendered_as_mandatory_context():
    compilation = compile_default()
    content = compilation.packet.content

    assert "THEOREM KERNEL (canonical, mandatory)" in content
    assert KERNEL.statement in content
    assert "n is a natural number" in content
    assert "the proof is constructive" in content
    assert content.index("THEOREM KERNEL") < content.index("OBSERVED FAILURES")


def test_manifest_records_theorem_kernel_provenance():
    compilation = compile_default()
    decision = compilation.result.selection_decisions[0]

    assert decision.source_id == KERNEL.kernel_id
    assert decision.source_version == VERSION
    assert decision.status == KERNEL.status.value
    assert decision.included is True
    assert decision.reason == "theorem kernel"
    assert compilation.manifest["sources"][0] == {
        "source_id": KERNEL.kernel_id,
        "source_version": VERSION,
        "status": KERNEL.status.value,
        "included": True,
        "reason": "theorem kernel",
    }


def test_obstruction_task_requires_a_kernel_and_version():
    with pytest.raises(ContextValidationError, match="kernel must be"):
        ObstructionTask(object(), VERSION)  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="kernel_version"):
        ObstructionTask(KERNEL, "")


def test_repeat_compilation_is_deterministic():
    first = compile_default()
    second = compile_default()

    assert first.packet.content == second.packet.content
    assert first.packet_digest == second.packet_digest
    assert first.manifest == second.manifest


def test_selection_keeps_a_small_set_and_dedupes():
    case = scenario()
    compilation = compile_obstruction(
        case.request, case.bundle, case.reader, case.task, max_failed_states=4
    )

    selected = compilation.payload.selected_state_ids
    assert selected[0] == "p1/fs-10"
    assert "p1/fs-14" not in selected
    assert set(selected) == {"p1/fs-10", "p1/fs-11", "p1/fs-12"}
    assert len(selected) == 3

    capped = compile_obstruction(
        case.request, case.bundle, case.reader, case.task, max_failed_states=2
    )
    assert capped.payload.selected_state_ids == ("p1/fs-10", "p1/fs-11")


def test_closed_and_tainted_states_require_recorded_obstruction_names():
    entries = default_entries()
    for source_type, source_id, body in entries:
        if source_id == "p1/fs-12":
            body["status"] = "formally-closed"
        if source_id == "p1/obs-2":
            body["formal_state_ids"] = ["p1/fs-10"]

    bundle, artifacts = make_bundle(entries)
    request = make_request()
    compilation = compile_obstruction(
        request,
        bundle,
        MappingArtifactReader(artifacts),
        make_task(),
        max_failed_states=4,
    )

    assert compilation.payload.selected_state_ids == ("p1/fs-10", "p1/fs-11")
    selected_attempts = {
        attempt.attempt_id
        for failure in compilation.payload.observed
        for attempt in failure.attempts
    }
    assert "p1/ta-8" not in selected_attempts
    assert "p1/ta-9" not in selected_attempts

    for source_type, source_id, body in entries:
        if source_id == "p1/obs-2":
            body["formal_state_ids"] = [
                "p1/fs-10",
                "p1/fs-12",
                "p1/fs-13",
            ]

    bundle, artifacts = make_bundle(entries)
    named = compile_obstruction(
        request,
        bundle,
        MappingArtifactReader(artifacts),
        make_task(),
        max_failed_states=4,
    )

    assert set(named.payload.selected_state_ids) == {
        "p1/fs-10",
        "p1/fs-11",
        "p1/fs-12",
        "p1/fs-13",
    }


def test_exact_local_context_is_preserved_verbatim():
    compilation = compile_default()
    failure = compilation.payload.observed[0]

    assert failure.state_id == "p1/fs-10"
    assert failure.hypotheses == (HYP_10,)
    assert failure.target == TARGET_10
    assert failure.status is FormalStateStatus.OPEN
    assert failure.attempts[0].attempt_id == "p1/ta-6"
    assert failure.attempts[0].premises == (
        "Finset.sum_range_succ",
        "Nat.sum_range_add",
    )
    assert failure.attempts[0].executor_result is ExecutorResult.LEAN_REJECTED
    assert failure.diagnostics[0].artifact_hash == DIAG_1
    assert failure.diagnostics[0].occurrences == 12
    assert failure.diagnostics[0].text == (
        "unknown identifier: Finset.sum_range_succ"
    )

    content = compilation.packet.content
    assert HYP_10 in content
    assert TARGET_10 in content
    assert "Finset.sum_range_succ, Nat.sum_range_add" in content
    assert "unknown identifier: Finset.sum_range_succ" in content


def test_observed_failures_and_suspected_causes_are_distinct():
    compilation = compile_default()
    payload = compilation.payload

    assert payload.observed
    assert payload.suspected
    assert all(isinstance(cause.basis, EvidenceKind) for cause in payload.suspected)
    observed_targets = {failure.target for failure in payload.observed}
    assert not any(
        cause.hypothesis in observed_targets for cause in payload.suspected
    )

    content = compilation.packet.content
    observed_block = content.split("OBSERVED FAILURES")[1].split("ATTEMPTED TACTICS")[0]
    suspected_block = content.split("SUSPECTED CAUSES")[1].split("RESEARCH QUESTIONS")[0]
    assert "RESEARCH QUESTIONS" in content
    assert TARGET_10 in observed_block
    assert TARGET_10 not in suspected_block
    assert "(unverified, not observed)" in content
    assert "(recorded, exact)" in content


def test_research_questions_include_supplied_and_derived():
    supplied = "Would a different induction on n avoid this step?"
    case = scenario()
    compilation = compile_obstruction(
        case.request,
        case.bundle,
        case.reader,
        case.task,
        research_questions=(supplied,),
    )
    questions = compilation.payload.research_questions

    assert questions[0] == supplied
    assert any("p1/fs-10" in question for question in questions)
    assert any(TARGET_10 in question for question in questions)
    assert any("Finset.sum_range_succ" in question for question in questions)
    assert any("unknown identifier: Finset.sum_range_succ" in question for question in questions)
    assert any("unknown-identifier" in question for question in questions)
    assert len(set(questions)) == len(questions)
    for question in questions:
        assert question in compilation.packet.content


def test_supplied_suspected_cause_is_kept_and_labelled():
    case = scenario()
    cause = SuspectedCause(
        hypothesis="The library pin predates the lemma used by rw.",
        basis=EvidenceKind.CRITIC_REVIEW,
        note="checked against the pinned snapshot",
        source_ids=("p1/obs-2",),
    )
    compilation = compile_obstruction(
        case.request,
        case.bundle,
        case.reader,
        case.task,
        suspected_causes=(cause,),
    )

    assert compilation.payload.suspected[0] is cause
    rendered = compilation.packet.content.split("SUSPECTED CAUSES")[1]
    assert cause.hypothesis in rendered
    assert "critic-review" in rendered


def test_manifest_records_kernel_and_every_bundle_source():
    compilation = compile_default()
    decisions = compilation.result.selection_decisions
    expected_ids = {
        source.source_id for source in compilation.payload.source_artifacts
    }
    expected_ids.add(KERNEL.kernel_id)

    assert {decision.source_id for decision in decisions} == expected_ids
    assert compilation.manifest["sources"] == [
        {
            "source_id": decision.source_id,
            "source_version": decision.source_version,
            "status": decision.status,
            "included": decision.included,
            "reason": decision.reason,
        }
        for decision in decisions
    ]

    by_id = {decision.source_id: decision for decision in decisions}
    assert by_id["p1/fs-10"].included
    assert by_id["p1/fs-10"].reason == "selected failed state"
    assert by_id["p1/fs-10"].status == "open"
    assert not by_id["p1/fs-13"].included
    assert by_id["p1/fs-13"].reason == "not selected: beyond the small relevant set"
    assert by_id["p1/ta-6"].included
    assert by_id["p1/ta-6"].status == "lean-rejected"
    assert not by_id["p1/ta-9"].included
    assert by_id["p1/ta-9"].reason == "attempt of an unselected state"
    assert by_id["p1/dg-1"].included
    assert not by_id["p1/dg-2"].included
    assert by_id["p1/obs-2"].included
    assert by_id["p1/obs-2"].reason == "obstruction and run identity"
    assert by_id["p1/fr-4"].included
    assert by_id["p1/fr-4"].status == "stagnated"


def test_payload_references_every_complete_source_artifact():
    compilation = compile_default()
    content = compilation.packet.content

    for source in compilation.payload.source_artifacts:
        assert source.source_id in content
        assert source.content_hash in content


def test_to_schema_dict_validates_against_the_shared_schema():
    payload = compile_default().payload
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(payload.to_schema_dict()))

    assert errors == []


def test_to_dict_carries_the_full_payload():
    payload = compile_default().payload
    full = payload.to_dict()
    strict = payload.to_schema_dict()

    assert full["research_questions"] == list(payload.research_questions)
    assert len(full["source_artifacts"]) == len(payload.source_artifacts)
    assert full["observed"][0]["target"] == TARGET_10
    assert "research_questions" not in strict
    assert "source_artifacts" not in strict
    assert "observed" not in strict
    assert strict["formal_state_ids"] == list(payload.selected_state_ids)


def test_budget_is_respected_and_cheapest_sections_drop_first():
    counter = CharHeuristicTokenCounter()
    case = scenario()
    full = compile_obstruction(case.request, case.bundle, case.reader, case.task)
    assert counter.count(full.packet.content) <= 1200

    budget = 700
    case = scenario(text_budget=TextBudget(budget))
    clipped = compile_obstruction(
        case.request, case.bundle, case.reader, case.task
    )

    assert counter.count(clipped.packet.content) <= budget
    assert clipped.manifest["budgets"] == {"text_tokens": budget}
    assert "SOURCE ARTIFACTS" not in clipped.packet.content
    assert KERNEL.statement in clipped.packet.content
    assert all(
        assumption in clipped.packet.content for assumption in KERNEL.assumptions
    )
    assert "OBSERVED FAILURES" in clipped.packet.content
    reasons = {decision.reason for decision in clipped.result.selection_decisions}
    assert "excluded: omitted to fit text budget" in reasons


def test_tiny_budget_cannot_truncate_the_mandatory_kernel():
    case = scenario(text_budget=TextBudget(60))
    with pytest.raises(
        ContextValidationError,
        match="mandatory obstruction sections exceed the text budget",
    ):
        compile_obstruction(case.request, case.bundle, case.reader, case.task)


def test_packet_kind_must_be_obstruction():
    case = scenario(packet_kind=PacketKind.RESEARCH)
    with pytest.raises(ContextValidationError, match="packet_kind"):
        compile_obstruction(case.request, case.bundle, case.reader, case.task)


def test_missing_source_artifact_is_reported():
    case = scenario()
    reader = MappingArtifactReader({})
    with pytest.raises(ContextValidationError, match="missing source artifact"):
        compile_obstruction(case.request, case.bundle, reader, case.task)


def test_unknown_source_type_is_rejected():
    entries = default_entries()
    entries.append(("claim", "p1/c-1", {"claim_id": "p1/c-1"}))
    bundle, artifacts = make_bundle(entries)
    with pytest.raises(ContextValidationError, match="unknown source_type"):
        compile_obstruction(
            make_request(), bundle, MappingArtifactReader(artifacts), make_task()
        )


def test_malformed_state_body_is_rejected():
    entries = default_entries()
    entries[0] = (
        "formal-state",
        "p1/fs-10",
        {**entries[0][2], "status": "definitely-open"},
    )
    bundle, artifacts = make_bundle(entries)
    with pytest.raises(ContextValidationError, match="status"):
        compile_obstruction(
            make_request(), bundle, MappingArtifactReader(artifacts), make_task()
        )


def test_malformed_vocabulary_value_is_a_validation_error():
    entries = default_entries()
    entries[0] = (
        "formal-state",
        "p1/fs-10",
        {**entries[0][2], "status": ["open"]},
    )
    bundle, artifacts = make_bundle(entries)
    with pytest.raises(ContextValidationError, match="status"):
        compile_obstruction(
            make_request(), bundle, MappingArtifactReader(artifacts), make_task()
        )


def test_identity_is_required_without_a_recorded_obstruction_or_run():
    entries = [
        entry for entry in default_entries() if entry[0] not in {"obstruction", "formal-run"}
    ]
    bundle, artifacts = make_bundle(entries)
    with pytest.raises(ContextValidationError, match="formal_run_id"):
        compile_obstruction(
            make_request(), bundle, MappingArtifactReader(artifacts), make_task()
        )

    compilation = compile_obstruction(
        make_request(),
        bundle,
        MappingArtifactReader(artifacts),
        make_task(),
        formal_run_id="p1/fr-9",
        environment_hash=ENV_HASH,
    )
    assert compilation.payload.formal_run_id == "p1/fr-9"
    assert compilation.payload.obstruction_id == "p1/obs-1"


def test_bundle_without_failed_states_is_reported():
    entries = [
        entry
        for entry in default_entries()
        if entry[0] not in {"attempt", "obstruction", "formal-run"}
    ]
    bundle, artifacts = make_bundle(entries)
    with pytest.raises(ContextValidationError, match="no failed states"):
        compile_obstruction(
            make_request(),
            bundle,
            MappingArtifactReader(artifacts),
            make_task(),
            formal_run_id="p1/fr-9",
            environment_hash=ENV_HASH,
        )


def test_diagnostic_hash_must_match_the_source_reference():
    bundle, artifacts = make_bundle(default_entries())
    other_hash = "sha256:" + "ab" * 32
    mismatched = SourceBundle(
        "p1/bundle-obs",
        tuple(
            SourceRef(source.source_type, source.source_id, source.source_version, other_hash)
            if source.source_id == "p1/dg-1"
            else source
            for source in bundle.sources
        ),
    )
    with pytest.raises(ContextValidationError, match="artifact_hash"):
        compile_obstruction(
            make_request(), mismatched, MappingArtifactReader(artifacts), make_task()
        )


def test_max_failed_states_must_be_positive():
    case = scenario()
    with pytest.raises(ContextValidationError, match="max_failed_states"):
        compile_obstruction(
            case.request,
            case.bundle,
            case.reader,
            case.task,
            max_failed_states=0,
        )
