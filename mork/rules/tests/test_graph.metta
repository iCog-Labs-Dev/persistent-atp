;; test_graph.metta — Shared test graph for rule testing
;;
;; A reusable generic graph structure that can be imported by all rule tests.
;;
;; Graph structure:
;;
;;   a1 --ON_STATE--> s1 --PROPOSES--> m1
;;     \                          /
;;      `-- SUPPORTED_BY -> c1   /
;;                             '-- ON_MOVE --'
;;
;; Edges:
;;   e1: s1 -PROPOSES-> m1     (outgoing from s1; incoming to m1)
;;   e2: s1 -SUPPORTED_BY-> c1 (outgoing from s1; incoming to c1)
;;   e3: a1 -ON_STATE-> s1     (incoming to s1)
;;
;; Node c2 is isolated: no edges at all.
;; =========================================================================

;; Nodes
!(add-atom &mork (node "section1" "s1" "State"))
!(add-atom &mork (layer "section1" "s1" "committed"))
!(add-atom &mork (node "section1" "m1" "Move"))
!(add-atom &mork (layer "section1" "m1" "committed"))
!(add-atom &mork (node "section1" "c1" "Claim"))
!(add-atom &mork (layer "section1" "c1" "committed"))
!(add-atom &mork (node "section1" "a1" "Attempt"))
!(add-atom &mork (layer "section1" "a1" "committed"))
!(add-atom &mork (node "section1" "c2" "Claim"))
!(add-atom &mork (layer "section1" "c2" "committed"))

;; Edges: e1 (s1 -PROPOSES-> m1)
!(add-atom &mork (edge "section1" "e1" "PROPOSES" "s1" "m1"))
!(add-atom &mork (rev-edge "section1" "m1" "PROPOSES" "s1" "e1"))
!(add-atom &mork (layer "section1" "edge:e1" "committed"))

;; Edges: e2 (s1 -SUPPORTED_BY-> c1)
!(add-atom &mork (edge "section1" "e2" "SUPPORTED_BY" "s1" "c1"))
!(add-atom &mork (rev-edge "section1" "c1" "SUPPORTED_BY" "s1" "e2"))
!(add-atom &mork (layer "section1" "edge:e2" "committed"))

;; Edges: e3 (a1 -ON_STATE-> s1)
!(add-atom &mork (edge "section1" "e3" "ON_STATE" "a1" "s1"))
!(add-atom &mork (rev-edge "section1" "s1" "ON_STATE" "a1" "e3"))
!(add-atom &mork (layer "section1" "edge:e3" "committed"))
