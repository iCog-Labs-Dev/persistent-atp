;; test_frontier.metta — Tests for Sections 2-3: eligible frontier and closure

!(import! &self ../frontier)

!(mm2-exec &mork 1)

;; Import shared test graph
!(import! &self ./test_graph.metta)

;; =========================================================================
;; Extended test data for frontier tests
;;
;; We enhance the shared test graph with status fields:
;;
;; States:
;;   s1: status="open"
;;   s2: status="tainted" (blocks its moves)
;;   s3: status="formally-closed" (blocks its moves)
;;
;; Moves:
;;   m1: status="open" (eligible if state allows)
;;   m2: status="reopened" (eligible if state allows)
;;   m3: status="refuted" (excluded)
;;   m4: status="leased" (excluded)
;;   m5: status="dominated" (excluded)
;;   m6: status="exhausted" (excluded)
;;   m7: status="formally-closed" (solves state s1)
;;
;; Edges:
;;   s1 -PROPOSES-> [m1, m2, m3, m4, m5, m6, m7]
;;   s2 -PROPOSES-> [m8]
;;   s3 -PROPOSES-> [m9]
;;   m1 -REQUIRES-> [sg1, sg2]
;;   m7 -REQUIRES-> [sg3]
;; =========================================================================

;; Additional states
!(add-atom &mork (node "test-proof" "s2" "State"))
!(add-atom &mork (layer "test-proof" "s2" "committed"))
!(add-atom &mork (node "test-proof" "s3" "State"))
!(add-atom &mork (layer "test-proof" "s3" "committed"))

;; Additional moves
!(add-atom &mork (node "test-proof" "m2" "Move"))
!(add-atom &mork (layer "test-proof" "m2" "committed"))
!(add-atom &mork (node "test-proof" "m3" "Move"))
!(add-atom &mork (layer "test-proof" "m3" "committed"))
!(add-atom &mork (node "test-proof" "m4" "Move"))
!(add-atom &mork (layer "test-proof" "m4" "committed"))
!(add-atom &mork (node "test-proof" "m5" "Move"))
!(add-atom &mork (layer "test-proof" "m5" "committed"))
!(add-atom &mork (node "test-proof" "m6" "Move"))
!(add-atom &mork (layer "test-proof" "m6" "committed"))
!(add-atom &mork (node "test-proof" "m7" "Move"))
!(add-atom &mork (layer "test-proof" "m7" "committed"))
!(add-atom &mork (node "test-proof" "m8" "Move"))
!(add-atom &mork (layer "test-proof" "m8" "committed"))
!(add-atom &mork (node "test-proof" "m9" "Move"))
!(add-atom &mork (layer "test-proof" "m9" "committed"))

;; Subgoals for AND logic tests
!(add-atom &mork (node "test-proof" "sg1" "State"))
!(add-atom &mork (layer "test-proof" "sg1" "committed"))
!(add-atom &mork (node "test-proof" "sg2" "State"))
!(add-atom &mork (layer "test-proof" "sg2" "committed"))
!(add-atom &mork (node "test-proof" "sg3" "State"))
!(add-atom &mork (layer "test-proof" "sg3" "committed"))
!(add-atom &mork (node "test-proof" "sg4" "State"))
!(add-atom &mork (layer "test-proof" "sg4" "committed"))

;; State statuses
!(add-atom &mork (field "test-proof" "s1" "status" "open"))
!(add-atom &mork (field "test-proof" "s2" "status" "tainted"))
!(add-atom &mork (field "test-proof" "s3" "status" "formally-closed"))

;; Move statuses
!(add-atom &mork (field "test-proof" "m1" "status" "open"))
!(add-atom &mork (field "test-proof" "m2" "status" "reopened"))
!(add-atom &mork (field "test-proof" "m3" "status" "refuted"))
!(add-atom &mork (field "test-proof" "m4" "status" "leased"))
!(add-atom &mork (field "test-proof" "m5" "status" "dominated"))
!(add-atom &mork (field "test-proof" "m6" "status" "exhausted"))
!(add-atom &mork (field "test-proof" "m7" "status" "formally-closed"))
!(add-atom &mork (field "test-proof" "m8" "status" "open"))
!(add-atom &mork (field "test-proof" "m9" "status" "open"))

;; Subgoal statuses
!(add-atom &mork (field "test-proof" "sg1" "status" "formally-closed"))
!(add-atom &mork (field "test-proof" "sg2" "status" "open"))
!(add-atom &mork (field "test-proof" "sg3" "status" "formally-closed"))
!(add-atom &mork (field "test-proof" "sg4" "status" "reopened"))

;; Proposed edges: s1 proposes m1..m7
!(add-atom &mork (edge "test-proof" "e4" "PROPOSES" "s1" "m2"))
!(add-atom &mork (rev-edge "test-proof" "m2" "PROPOSES" "s1" "e4"))
!(add-atom &mork (layer "test-proof" "edge:e4" "committed"))

!(add-atom &mork (edge "test-proof" "e5" "PROPOSES" "s1" "m3"))
!(add-atom &mork (rev-edge "test-proof" "m3" "PROPOSES" "s1" "e5"))
!(add-atom &mork (layer "test-proof" "edge:e5" "committed"))

!(add-atom &mork (edge "test-proof" "e6" "PROPOSES" "s1" "m4"))
!(add-atom &mork (rev-edge "test-proof" "m4" "PROPOSES" "s1" "e6"))
!(add-atom &mork (layer "test-proof" "edge:e6" "committed"))

!(add-atom &mork (edge "test-proof" "e7" "PROPOSES" "s1" "m5"))
!(add-atom &mork (rev-edge "test-proof" "m5" "PROPOSES" "s1" "e7"))
!(add-atom &mork (layer "test-proof" "edge:e7" "committed"))

!(add-atom &mork (edge "test-proof" "e8" "PROPOSES" "s1" "m6"))
!(add-atom &mork (rev-edge "test-proof" "m6" "PROPOSES" "s1" "e8"))
!(add-atom &mork (layer "test-proof" "edge:e8" "committed"))

!(add-atom &mork (edge "test-proof" "e9" "PROPOSES" "s1" "m7"))
!(add-atom &mork (rev-edge "test-proof" "m7" "PROPOSES" "s1" "e9"))
!(add-atom &mork (layer "test-proof" "edge:e9" "committed"))

;; Proposed edge: s2 proposes m8
!(add-atom &mork (edge "test-proof" "e10" "PROPOSES" "s2" "m8"))
!(add-atom &mork (rev-edge "test-proof" "m8" "PROPOSES" "s2" "e10"))
!(add-atom &mork (layer "test-proof" "edge:e10" "committed"))

;; Proposed edge: s3 proposes m9
!(add-atom &mork (edge "test-proof" "e11" "PROPOSES" "s3" "m9"))
!(add-atom &mork (rev-edge "test-proof" "m9" "PROPOSES" "s3" "e11"))
!(add-atom &mork (layer "test-proof" "edge:e11" "committed"))

;; REQUIRES edges: m1 requires sg1, sg2
!(add-atom &mork (edge "test-proof" "e12" "REQUIRES" "m1" "sg1"))
!(add-atom &mork (rev-edge "test-proof" "sg1" "REQUIRES" "m1" "e12"))
!(add-atom &mork (layer "test-proof" "edge:e12" "committed"))

!(add-atom &mork (edge "test-proof" "e13" "REQUIRES" "m1" "sg2"))
!(add-atom &mork (rev-edge "test-proof" "sg2" "REQUIRES" "m1" "e13"))
!(add-atom &mork (layer "test-proof" "edge:e13" "committed"))

;; REQUIRES edge: m7 requires sg3
!(add-atom &mork (edge "test-proof" "e14" "REQUIRES" "m7" "sg3"))
!(add-atom &mork (rev-edge "test-proof" "sg3" "REQUIRES" "m7" "e14"))
!(add-atom &mork (layer "test-proof" "edge:e14" "committed"))


;; 2.1 has-field? tests

!(test
   (has-field? "test-proof" "s1" "status" "open")
   True)

!(test
   (has-field? "test-proof" "s1" "status" "closed")
   False)

!(test
   (has-field? "test-proof" "m1" "status" "open")
   True)


;; 2.2 move-open? tests

!(test
   (move-open? "test-proof" "m1")
   True)

!(test
   (move-open? "test-proof" "m2")
   True)

!(test
   (move-open? "test-proof" "m3")
   False)


;; 2.3 move-excluded? tests

!(test
   (move-excluded? "test-proof" "m3")
   True)

!(test
   (move-excluded? "test-proof" "m4")
   True)

!(test
   (move-excluded? "test-proof" "m5")
   True)

!(test
   (move-excluded? "test-proof" "m6")
   True)

!(test
   (move-excluded? "test-proof" "m1")
   False)


;; 2.4 state-blocked? tests


!(test
   (state-blocked? "test-proof" "s1")
   False)

!(test
   (state-blocked? "test-proof" "s2")
   True)

!(test
   (state-blocked? "test-proof" "s3")
   True)


;; 2.5 is-eligible-move? tests

!(test
   (is-eligible-move? "test-proof" "s1" "m1")
   True)

!(test
   (is-eligible-move? "test-proof" "s1" "m2")
   True)

!(test
   (is-eligible-move? "test-proof" "s1" "m3")
   False)

!(test
   (is-eligible-move? "test-proof" "s2" "m8")
   False)

!(test
   (is-eligible-move? "test-proof" "s3" "m9")
   False)


;; 2.7 frontier-for-state tests


!(test
   (frontier-for-state "test-proof" "s1")
   ("m1" "m2"))

!(test
   (frontier-for-state "test-proof" "s2")
   ())

!(test
   (frontier-for-state "test-proof" "s3")
   ())


;; 2.9 is-frontier? tests


!(test
   (is-frontier? "test-proof" "m1")
   True)

!(test
   (is-frontier? "test-proof" "m2")
   True)

!(test
   (is-frontier? "test-proof" "m8")
   False)


;; 3.1 state-solved? tests


!(test
   (state-solved? "test-proof" "s1")
   True)


!(test
   (state-solved? "test-proof" "s2")
   False)


;; 3.2 subgoal-open? tests

!(test
   (subgoal-open? "test-proof" "sg1")
   False)

!(test
   (subgoal-open? "test-proof" "sg2")
   True)

!(test
   (subgoal-open? "test-proof" "sg3")
   False)

!(test
   (subgoal-open? "test-proof" "sg4")
   True)


;; 3.3 open-subgoal? tests

!(test
   (open-subgoal? "test-proof" "m1")
   True)

!(test
   (open-subgoal? "test-proof" "m7")
   False)


;; 3.4 move-complete? tests

!(test
   (move-complete? "test-proof" "m1")
   False)

!(test
   (move-complete? "test-proof" "m7")
   True)

!(test
   (move-complete? "test-proof" "m2")
   True)
