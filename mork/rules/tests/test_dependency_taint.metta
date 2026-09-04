;; test_dependency_taint.metta — Tests for Sections 4-5: reachability and taint

!(import! &self ../rules/frontier)
!(import! &self ../rules/dependency-taint)

!(mm2-exec &mork 1)

;; Import shared test graph
!(import! &self ./test_graph.metta)

;; =========================================================================
;; Extended test data for dependency-taint tests
;;
;; Graph structure (arrows show DEPENDS_ON direction, meaning "depends on"):
;;
;;   c1 ← c2 ← c3 ← c4     (chain 1, clean)
;;   c5 ← c6 ← c7           (chain 2, c5 is refuted — taint source)
;;
;; States:
;;   s1: formally-closed, uses c6  (should reopen when c5 refuted)
;;   s2: formally-closed, uses c7  (should reopen when c5 refuted)
;;   s3: open, uses c6             (already open, no change)
;;   s4: formally-closed, uses c1  (not tainted, no change)
;; =========================================================================

;; Additional claims
!(add-atom &mork (node "section1" "c3" "Claim"))
!(add-atom &mork (layer "section1" "c3" "committed"))
!(add-atom &mork (node "section1" "c4" "Claim"))
!(add-atom &mork (layer "section1" "c4" "committed"))
!(add-atom &mork (node "section1" "c5" "Claim"))
!(add-atom &mork (layer "section1" "c5" "committed"))
!(add-atom &mork (node "section1" "c6" "Claim"))
!(add-atom &mork (layer "section1" "c6" "committed"))
!(add-atom &mork (node "section1" "c7" "Claim"))
!(add-atom &mork (layer "section1" "c7" "committed"))

;; Claim statuses
!(add-atom &mork (field "section1" "c5" "status" "refuted"))

;; DEPENDS_ON edges: c2 depends on c1
!(add-atom &mork (edge "section1" "e20" "DEPENDS_ON" "c2" "c1"))
!(add-atom &mork (rev-edge "section1" "c1" "DEPENDS_ON" "c2" "e20"))
!(add-atom &mork (layer "section1" "edge:e20" "committed"))

;; DEPENDS_ON edges: c3 depends on c2
!(add-atom &mork (edge "section1" "e21" "DEPENDS_ON" "c3" "c2"))
!(add-atom &mork (rev-edge "section1" "c2" "DEPENDS_ON" "c3" "e21"))
!(add-atom &mork (layer "section1" "edge:e21" "committed"))

;; DEPENDS_ON edges: c4 depends on c3
!(add-atom &mork (edge "section1" "e22" "DEPENDS_ON" "c4" "c3"))
!(add-atom &mork (rev-edge "section1" "c3" "DEPENDS_ON" "c4" "e22"))
!(add-atom &mork (layer "section1" "edge:e22" "committed"))

;; DEPENDS_ON edges: c6 depends on c5
!(add-atom &mork (edge "section1" "e23" "DEPENDS_ON" "c6" "c5"))
!(add-atom &mork (rev-edge "section1" "c5" "DEPENDS_ON" "c6" "e23"))
!(add-atom &mork (layer "section1" "edge:e23" "committed"))

;; DEPENDS_ON edges: c7 depends on c6
!(add-atom &mork (edge "section1" "e24" "DEPENDS_ON" "c7" "c6"))
!(add-atom &mork (rev-edge "section1" "c6" "DEPENDS_ON" "c7" "e24"))
!(add-atom &mork (layer "section1" "edge:e24" "committed"))

;; Additional states
!(add-atom &mork (node "section1" "s2" "State"))
!(add-atom &mork (layer "section1" "s2" "committed"))
!(add-atom &mork (node "section1" "s3" "State"))
!(add-atom &mork (layer "section1" "s3" "committed"))
!(add-atom &mork (node "section1" "s4" "State"))
!(add-atom &mork (layer "section1" "s4" "committed"))

;; State statuses
!(add-atom &mork (field "section1" "s1" "status" "formally-closed"))
!(add-atom &mork (field "section1" "s2" "status" "formally-closed"))
!(add-atom &mork (field "section1" "s3" "status" "open"))
!(add-atom &mork (field "section1" "s4" "status" "formally-closed"))

;; USES_CLAIM edges: s1 uses c6
!(add-atom &mork (edge "section1" "e25" "USES_CLAIM" "s1" "c6"))
!(add-atom &mork (rev-edge "section1" "c6" "USES_CLAIM" "s1" "e25"))
!(add-atom &mork (layer "section1" "edge:e25" "committed"))

;; USES_CLAIM edges: s2 uses c7
!(add-atom &mork (edge "section1" "e26" "USES_CLAIM" "s2" "c7"))
!(add-atom &mork (rev-edge "section1" "c7" "USES_CLAIM" "s2" "e26"))
!(add-atom &mork (layer "section1" "edge:e26" "committed"))

;; USES_CLAIM edges: s3 uses c6
!(add-atom &mork (edge "section1" "e27" "USES_CLAIM" "s3" "c6"))
!(add-atom &mork (rev-edge "section1" "c6" "USES_CLAIM" "s3" "e27"))
!(add-atom &mork (layer "section1" "edge:e27" "committed"))

;; USES_CLAIM edges: s4 uses c1
!(add-atom &mork (edge "section1" "e28" "USES_CLAIM" "s4" "c1"))
!(add-atom &mork (rev-edge "section1" "c1" "USES_CLAIM" "s4" "e28"))
!(add-atom &mork (layer "section1" "edge:e28" "committed"))


;; =========================================================================
;; 4.1 direct-depends? tests
;; =========================================================================

;; c2 directly depends on c1
!(test
   (direct-depends? "section1" "c2" "c1")
   True)

;; c1 does NOT depend on c2 (wrong direction)
!(test
   (direct-depends? "section1" "c1" "c2")
   False)

;; c3 does NOT directly depend on c1 (it depends on c2)
!(test
   (direct-depends? "section1" "c3" "c1")
   False)


;; =========================================================================
;; 4.2 reaches? tests
;; =========================================================================

;; Base case: every claim reaches itself
!(test
   (reaches? "section1" "c1" "c1")
   True)

!(test
   (reaches? "section1" "c5" "c5")
   True)

;; Direct dependency
!(test
   (reaches? "section1" "c2" "c1")
   True)

;; Transitive dependency (2 hops: c3 → c2 → c1)
!(test
   (reaches? "section1" "c3" "c1")
   True)

;; Transitive dependency (3 hops: c4 → c3 → c2 → c1)
!(test
   (reaches? "section1" "c4" "c1")
   True)

;; Wrong direction: c1 does NOT reach c4
!(test
   (reaches? "section1" "c1" "c4")
   False)

;; Direct dependency in chain 2
!(test
   (reaches? "section1" "c6" "c5")
   True)

;; Transitive dependency in chain 2 (c7 → c6 → c5)
!(test
   (reaches? "section1" "c7" "c5")
   True)

;; Cross-chain: c5 does NOT reach c1 (different chains)
!(test
   (reaches? "section1" "c5" "c1")
   False)

;; Cross-chain: c7 does NOT reach c1
!(test
   (reaches? "section1" "c7" "c1")
   False)


;; =========================================================================
;; 4.3 deps-of tests
;; =========================================================================

!(test
   (collapse (deps-of "section1" "c3"))
   ("c2"))

!(test
   (collapse (deps-of "section1" "c2"))
   ("c1"))

;; c1 has no dependencies
!(test
   (collapse (deps-of "section1" "c1"))
   ())

!(test
   (collapse (deps-of "section1" "c6"))
   ("c5"))

;; c5 has no dependencies
!(test
   (collapse (deps-of "section1" "c5"))
   ())


;; =========================================================================
;; 4.7 depends-on tests (transitive dependents)
;; =========================================================================

;; Everything that depends on c1: c2, c3, c4
!(test
   (collapse (depends-on "section1" "c1"))
   (("c2" "c3" "c4")))

;; Everything that depends on c5: c6, c7
!(test
   (collapse (depends-on "section1" "c5"))
   (("c6" "c7")))

;; Nothing depends on c7
!(test
   (collapse (depends-on "section1" "c7"))
   (()))

;; Nothing depends on c4 (end of chain 1)
!(test
   (collapse (depends-on "section1" "c4"))
   (()))


;; =========================================================================
;; 4.8 depends-on-chain tests (transitive dependencies)
;; =========================================================================

;; Everything c4 depends on: c3, c2, c1
!(test
   (collapse (depends-on-chain "section1" "c4"))
   (("c1" "c2" "c3")))

;; c1 depends on nothing
!(test
   (collapse (depends-on-chain "section1" "c1"))
   (()))

;; c6 depends on c5
!(test
   (collapse (depends-on-chain "section1" "c6"))
   (("c5")))

;; c7 depends on c6, c5
!(test
   (collapse (depends-on-chain "section1" "c7"))
   (("c5" "c6")))


;; =========================================================================
;; 4.9 would-cycle? tests
;; =========================================================================

;; Adding c1→c4 would cycle: c4 already reaches c1
!(test
   (would-cycle? "section1" "c1" "c4")
   True)

;; Adding c5→c7 would cycle: c7 already reaches c5
!(test
   (would-cycle? "section1" "c5" "c7")
   True)

;; Adding c1→c5 would NOT cycle: c5 does not reach c1
!(test
   (would-cycle? "section1" "c1" "c5")
   False)

;; Adding c4→c1 would cycle: c1 does not reach c4 yet, but wait —
;; actually c4 depends on c3 depends on c2 depends on c1, so
;; c1 does NOT reach c4. So adding c4→c1 would NOT cycle.
!(test
   (would-cycle? "section1" "c4" "c1")
   False)

;; Adding c1→c2 would cycle: c2 already reaches c1
!(test
   (would-cycle? "section1" "c1" "c2")
   True)


;; =========================================================================
;; 5.1 taint-cone tests
;; =========================================================================

;; c5 is refuted: taint cone should include c6 and c7 (claims that
;; transitively depend on c5). The comment says "other than the root"
;; but the current code does NOT exclude the root — if the test fails
;; with c5 included, that's the bug to fix.
!(test
   (collapse (taint-cone "section1" "c5"))
   (("c6" "c7")))

;; c1 is not refuted: empty taint cone
!(test
   (collapse (taint-cone "section1" "c1"))
   (()))       

;; c6 is not refuted: empty taint cone
!(test
   (collapse (taint-cone "section1" "c6"))
   (()))

;; c2 is not refuted: empty taint cone
!(test
   (collapse (taint-cone "section1" "c2"))
   (()))


;; =========================================================================
;; 5.5 affected-states tests
;; =========================================================================

;; c5 is refuted: s1 and s2 should be affected
;;   s1: formally-closed + uses c6 (c6 reaches c5) → REOPEN
;;   s2: formally-closed + uses c7 (c7 reaches c5) → REOPEN
;;   s3: open + uses c6 → already open, NOT affected
;;   s4: formally-closed + uses c1 → c1 does NOT reach c5, NOT affected
!(test
   (collapse (affected-states "section1" "c5"))
   (("s1" "s2")))

;; c1 is not refuted: no affected states
!(test
   (collapse (affected-states "section1" "c1"))
   (()))

;; c6 is not refuted: no affected states
!(test
   (collapse (affected-states "section1" "c6"))
   (()))

;; c2 is not refuted: no affected states
!(test
   (collapse (affected-states "section1" "c2"))
   (()))