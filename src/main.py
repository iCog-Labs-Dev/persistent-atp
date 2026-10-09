"""Driver for the even-sum proof pipeline demonstration.

Runs two demonstration flows against the commit gate and journal:

  Flow A  --  Full lifecycle
      Seed -> critic -> research -> formal-run -> replay -> formally-closed.

  Flow B  --  Stagnation & Adaptive Routing  (--run-obstruction)
      Demonstrates handling when formal search stagnates.

Usage::

    python src/main.py --verbose
    python src/main.py --verbose --run-obstruction
    python src/main.py --db-path scratch/poc.db --export-mork scratch/proof.mork
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from commit_gate.apply import apply_ops
from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from commit_gate.vocab import RunDisposition
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.formal_atp import FakeFormalATP
from mathproof.scheduler import GlobalScheduler


_COLOURS = {
    "SCHEDULER": "\033[36m",
    "DISPATCH":  "\033[32m",
    "GATE":      "\033[33m",
    "SOUNDNESS": "\033[35m",
    "ROUTING":   "\033[34m",
    "AUDIT":     "\033[32m",
    "FLOW":      "\033[1m",
    "ERROR":     "\033[31m",
}
_RESET = "\033[0m"


def _log(tag: str, msg: str, verbose: bool) -> None:
    if not verbose:
        return
    colour = _COLOURS.get(tag, "")
    print(f"{colour}[{tag}]{_RESET} {msg}")


def _seed_view(proof_id: str) -> MemoryView:
    view = MemoryView()
    view.add_node(
        f"{proof_id}/c-1",
        "Claim",
        {"status": "provisional", "text": "forall n : N, even n -> even (n + n)"},
    )
    view.add_node(
        f"{proof_id}/fd-1",
        "FormalDeclaration",
        {
            "status": "aligned",
            "lean_name": "even_sum",
            "lean_type": "forall n : N, Even n -> Even (n + n)",
            "lean_value": "by omega",
        },
    )
    view.add_node(
        f"{proof_id}/al-1",
        "Alignment",
        {"lifecycle": "reviewed", "verdict": "aligned"},
    )
    view.add_node(f"{proof_id}/rs-1", "ResearchState", {"status": "open"})
    view.add_edge("ALIGNS_CLAIM", f"{proof_id}/al-1", f"{proof_id}/c-1", f"{proof_id}/al-1-claim")
    view.add_edge("ALIGNS_DECLARATION", f"{proof_id}/al-1", f"{proof_id}/fd-1", f"{proof_id}/al-1-decl")
    return view


def _atp_flow_a(proof_id: str) -> FakeFormalATP:
    return FakeFormalATP(plan={f"{proof_id}/fr-1": [RunDisposition.PROVED_PENDING_REPLAY.value]})


def _atp_flow_b(proof_id: str) -> FakeFormalATP:
    return FakeFormalATP(plan={f"{proof_id}/fr-1": [RunDisposition.STAGNATED.value]})


def _dispatcher_critic(verbose: bool) -> ScriptedDispatcher:
    def _handle(lease, ctx):
        _log("DISPATCH", f"critic worker on {lease.selected_move_id}", verbose)
        return {"verdict": "critic-accepted", "actor": "llm-critic-0"}
    return ScriptedDispatcher({"critic": _handle})


def _dispatcher_research(proof_id: str, verbose: bool) -> ScriptedDispatcher:
    def _handle(lease, ctx):
        _log("DISPATCH", f"research worker on {lease.selected_move_id}", verbose)
        parent = (ctx.get("parent_state") or {}).get("id") or f"{proof_id}/rs-1"
        return {"parent_state_id": parent, "detail": "even-sum follows from linearity", "actor": "llm-explorer-0"}
    return ScriptedDispatcher({"llm-research": _handle})


def _make_maint(view: MemoryView, verbose: bool):
    def _cb(proposal, commit):
        if commit.accepted:
            apply_ops(view, proposal.ops)
            _log("GATE", f"revision {commit.revision} committed ({len(proposal.ops)} ops)", verbose)
        else:
            _log("ERROR", f"commit rejected: {[str(r) for r in commit.rejections]}", verbose)
    return _cb


def run_flow_a(
    proof_id: str,
    view: MemoryView,
    gate: CommitGate,
    scheduler: GlobalScheduler,
    store: JournalStore,
    verbose: bool,
) -> None:
    _log("FLOW", "=== Flow A: Happy Path ===", verbose)

    atp = _atp_flow_a(proof_id)
    maint = _make_maint(view, verbose)

    _log("SCHEDULER", "issuing critic lease ...", verbose)
    d = run_cycle(
        proof_id,
        view=view, gate=gate, scheduler=scheduler,
        dispatcher=_dispatcher_critic(verbose),
        worker_class="critic", maintenance=maint,
    )
    _log("GATE", f"accepted={d.accepted}  move={d.selected_move_id}", verbose)

    _log("SCHEDULER", "issuing research lease ...", verbose)
    d = run_cycle(
        proof_id,
        view=view, gate=gate, scheduler=scheduler,
        dispatcher=_dispatcher_research(proof_id, verbose),
        worker_class="llm-research", maintenance=maint,
    )
    _log("GATE", f"accepted={d.accepted}  move={d.selected_move_id}", verbose)

    _log("SCHEDULER", "issuing formal-atp lease ...", verbose)
    d = run_cycle(
        proof_id,
        view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}),
        worker_class="formal-atp",
        adapters={"formal-atp": atp},
        maintenance=maint,
        auto_replay=True,
    )
    _log("ROUTING", f"routing={d.routing}", verbose)
    if d.routing and d.routing.action == "hold-for-replay":
        _log("SOUNDNESS", "independent replay triggered by auto_replay=True", verbose)

    _log("AUDIT", "verifying journal hash chain ...", verbose)
    try:
        n = store.verify_chain(proof_id)
        _log("AUDIT", f"chain valid: {n} events checked", verbose)
    except Exception as exc:
        print(f"[ERROR] hash chain verification failed: {exc}", file=sys.stderr)
        sys.exit(1)

    _log("FLOW", "Flow A complete -- formally-closed via replay", verbose)


def run_flow_b(
    proof_id: str,
    view: MemoryView,
    gate: CommitGate,
    scheduler: GlobalScheduler,
    store: JournalStore,
    verbose: bool,
) -> None:
    _log("FLOW", "=== Flow B: Stagnation & Adaptive Routing ===", verbose)

    atp = _atp_flow_b(proof_id)
    maint = _make_maint(view, verbose)

    _log("SCHEDULER", "issuing critic lease ...", verbose)
    run_cycle(
        proof_id,
        view=view, gate=gate, scheduler=scheduler,
        dispatcher=_dispatcher_critic(verbose),
        worker_class="critic", maintenance=maint,
    )

    _log("SCHEDULER", "issuing research lease ...", verbose)
    run_cycle(
        proof_id,
        view=view, gate=gate, scheduler=scheduler,
        dispatcher=_dispatcher_research(proof_id, verbose),
        worker_class="llm-research", maintenance=maint,
    )

    _log("SCHEDULER", "issuing formal-atp lease (stagnated run) ...", verbose)
    d = run_cycle(
        proof_id,
        view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}),
        worker_class="formal-atp",
        adapters={"formal-atp": atp},
        maintenance=maint,
    )
    action = d.routing.action if d.routing else None
    _log("ROUTING", f"routing action={action}", verbose)
    _log("FLOW", f"stagnation handled -- routing dispatched {action!r}", verbose)

    _log("AUDIT", "verifying journal hash chain ...", verbose)
    try:
        n = store.verify_chain(proof_id)
        _log("AUDIT", f"chain valid: {n} events checked", verbose)
    except Exception as exc:
        print(f"[ERROR] hash chain verification failed: {exc}", file=sys.stderr)
        sys.exit(1)

    _log("FLOW", "Flow B complete", verbose)


def export_mork(store: JournalStore, path: str, verbose: bool) -> None:
    import json as _json

    _mork_root = Path(__file__).parent.parent / "mork"
    if str(_mork_root) not in sys.path:
        sys.path.insert(0, str(_mork_root))

    try:
        from projector.core import Projector
    except ImportError as exc:
        _log("ERROR", f"could not import mork projector: {exc}", verbose=True)
        return

    rows = store._conn.execute(
        "SELECT payload FROM journal ORDER BY revision ASC"
    ).fetchall()
    events = [_json.loads(row["payload"]) for row in rows]

    projector = Projector()
    commands = projector.process_event_journal({"events": events})

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(commands) + "\n", encoding="utf-8")
    _log("AUDIT", f"MORK atoms written to {out} ({len(commands)} commands)", verbose)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="poc-even-sum",
        description="Driver for the even-sum proof pipeline demonstration.",
    )
    parser.add_argument("--db-path", default=None, metavar="PATH",
                        help="SQLite journal path (default: in-memory).")
    parser.add_argument("--run-obstruction", action="store_true",
                        help="Also run Flow B: stagnation + adaptive routing demo.")
    parser.add_argument("--export-mork", default=None, metavar="PATH",
                        help="Write MORK atom projection to this file after each flow.")
    parser.add_argument("--verbose", action="store_true",
                        help="Colour-coded step-by-step output.")
    args = parser.parse_args(argv)

    db = args.db_path or ":memory:"

    pid_a = "even-sum"
    view_a = _seed_view(pid_a)
    store_a = JournalStore(db)
    run_flow_a(pid_a, view_a, CommitGate(view_a, store_a), GlobalScheduler(view_a, store_a), store_a, args.verbose)
    if args.export_mork:
        export_mork(store_a, args.export_mork, args.verbose)

    if args.run_obstruction:
        pid_b = "even-sum-stagnated"
        view_b = _seed_view(pid_b)
        store_b = JournalStore(db)
        run_flow_b(pid_b, view_b, CommitGate(view_b, store_b), GlobalScheduler(view_b, store_b), store_b, args.verbose)
        if args.export_mork:
            export_mork(store_b, args.export_mork + ".flow-b", args.verbose)

    if not args.verbose:
        print("PoC complete. Run with --verbose for step-by-step output.")


if __name__ == "__main__":
    main()
