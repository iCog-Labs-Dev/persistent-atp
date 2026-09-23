from __future__ import annotations

from typing import Any, Dict, List

from .constants import CLAIM_CONJECTURAL, MOVE_OPEN, STATE_KIND_OR


class ReplayMixin:
    """Journal replay: wipe-and-rebuild from event log.

    Expects the host class to provide every CRUD method called by
    ``_replay_event`` (init_proof, add_state, add_claim, etc.).
    """

    def wipe_and_rebuild(self, proof_id: str, events: List[Dict[str, Any]]) -> None:
        """Delete the proof's projection and replay the journal into it.

        Deliberately *not* one transaction: a full replay can be arbitrarily
        large, and the journal — not the graph — is the durability authority, so
        an interrupted rebuild is recovered by rebuilding again rather than by
        rollback. Each individual event is still applied atomically.
        """
        self._write_all(
            "wipe_and_rebuild",
            [("MATCH (n) WHERE n.proof_id = $pid DETACH DELETE n", {"pid": proof_id})],
        )
        for event in events:
            self._replay_event(proof_id, event)

    def _replay_event(self, proof_id: str, event: Dict[str, Any]) -> None:
        t = event.get("type")
        p = event.get("payload", {})
        evt = event.get("id", "")
        if t == "project_init":
            self.init_proof(proof_id, p.get("theorem_kernel", ""), event_id=evt)
        elif t == "state_added":
            st = p["state"]
            self.add_state(proof_id, st["id"], st["description"], st.get("parent"),
                           kind=st.get("kind", STATE_KIND_OR), assumptions=st.get("assumptions", ""),
                           event_id=evt)
        elif t == "claim_added":
            c = p["claim"]
            self.add_claim(proof_id, c["id"], c["statement"], c.get("status", CLAIM_CONJECTURAL), event_id=evt)
        elif t == "claim_dependency_added":
            self.add_claim_dependency(p["dependent_claim_id"], p["depends_on_claim_id"], proof_id, evt)
        elif t == "move_added":
            mv = p["move"]
            self.add_move(proof_id, mv["id"], mv["state_id"], mv["move_summary"],
                          mv.get("kind", "reduction"), mv.get("note", ""), event_id=evt,
                          status=mv.get("status", MOVE_OPEN))
        elif t == "subgoal_added":
            sg = p["subgoal"]
            self.add_required_subgoal(proof_id, p["move_id"], sg["id"], sg["description"],
                                      sg.get("parent"), event_id=evt)
        elif t == "move_updated":
            mv = p["move"]
            self.update_move_status(mv["id"], mv["status"], proof_id, evt)
        elif t == "attempt_recorded":
            a = p["attempt"]
            self.add_attempt(proof_id, a["id"], a["state_id"], a["move_summary"],
                             a.get("worker", "explorer"), a.get("note", ""),
                             a.get("move_id"), event_id=evt,
                             route_id=a.get("route_id"), model_persona=a.get("model_persona", ""),
                             disposition=a.get("disposition", ""), result_relation=a.get("result_relation", ""))
        elif t == "attempt_updated":
            a = p["attempt"]
            self.update_attempt(a["id"], a["status"], a.get("evidence", ""), proof_id, evt)
        elif t == "state_closed":
            self.close_state(p["state_id"], proof_id, p.get("reason", ""), evt)
        elif t == "state_reopened":
            self.reopen_state(proof_id, p["state_id"], p.get("reason", ""), evt)
        elif t == "claim_updated":
            self.update_claim_status(p["claim_id"], p["status"], proof_id, evt,
                                     p.get("reason", ""))
        elif t == "taint_propagated":
            self.propagate_taint(proof_id, p["claim_id"], evt, p.get("reason", ""))
        elif t == "route_added":
            r = p["route"]
            self.add_route(proof_id, r["id"], r["display_path"], evt)
        elif t == "context_added":
            c = p["context"]
            self.add_context(proof_id, c["id"], c.get("packet_hash", ""),
                             c.get("compiler_version", ""), c.get("token_budget", 0),
                             c.get("token_count", 0), evt)
        elif t == "artifact_added":
            a = p["artifact"]
            self.add_artifact(proof_id, a["id"], a.get("kind", "note"),
                              a.get("media_type", ""), a.get("sha256", ""),
                              a.get("filename", ""), evt)
        elif t == "artifact_linked":
            self.link_artifact(proof_id, p["attempt_id"], p["artifact_id"], evt)
        elif t == "attempt_route_linked":
            self.link_attempt_route(proof_id, p["attempt_id"], p["route_id"], evt)
        elif t == "attempt_context_linked":
            self.link_attempt_context(proof_id, p["attempt_id"], p["context_id"], evt)
        elif t == "claim_produced":
            self.link_produced_claim(proof_id, p["attempt_id"], p["claim_id"], evt)
        elif t == "critique_added":
            c = p["critique"]
            self.add_critique(proof_id, c["id"], p["attempt_id"], c["verdict"],
                              c.get("reason", ""), c.get("critic_worker", "critic"), evt)
        elif t == "experiment_added":
            e = p["experiment"]
            self.add_experiment(proof_id, e["id"], p["attempt_id"], e["question"],
                                e.get("status", "ran"), evt)
        elif t == "verification_added":
            v = p["verification"]
            self.add_verification(proof_id, v["id"], p["attempt_id"], p["claim_id"],
                                  v.get("kind", "lean"), v.get("status", "pending"),
                                  v.get("lean_name", ""), v.get("toolchain_hash", ""), evt)
        elif t == "bypass_added":
            self.add_relation(proof_id, "BYPASSES", p["move_id"], p["state_id"], evt, p.get("route_id", ""))
        elif t == "relation_added":
            self.add_relation(proof_id, p["rel"], p["from_id"], p["to_id"], evt, p.get("route_id", ""))
        elif t == "concept_added":
            c = p["concept"]
            self.add_concept(proof_id, c["id"], c["name"], c.get("mechanism_tags", ""), evt)
        elif t == "hypothesis_added":
            h = p["hypothesis"]
            self.add_hypothesis(proof_id, h["id"], h["kind"], h["target_state_id"],
                                h.get("falsification_test", ""), h.get("novelty", 0.0),
                                h.get("abductive_strength", 0.0), h.get("cost", 0.0),
                                h.get("risk", 0.0), h.get("lifecycle_status", "queued"), evt)
        elif t == "state_claim_link_added":
            self.link_state_claim(proof_id, p["state_id"], p["claim_id"], evt)
