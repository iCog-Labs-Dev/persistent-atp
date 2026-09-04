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
!(add-atom &mork (node "section1" "s2" "State"))
!(add-atom &mork (layer "section1" "s2" "committed"))
!(add-atom &mork (node "section1" "s3" "State"))
!(add-atom &mork (layer "section1" "s3" "committed"))

;; Additional moves
!(add-atom &mork (node "section1" "m2" "Move"))
!(add-atom &mork (layer "section1" "m2" "committed"))
!(add-atom &mork (node "section1" "m3" "Move"))
!(add-atom &mork (layer "section1" "m3" "committed"))
!(add-atom &mork (node "section1" "m4" "Move"))
!(add-atom &mork (layer "section1" "m4" "committed"))
!(add-atom &mork (node "section1" "m5" "Move"))
!(add-atom &mork (layer "section1" "m5" "committed"))
!(add-atom &mork (node "section1" "m6" "Move"))
!(add-atom &mork (layer "section1" "m6" "committed"))
!(add-atom &mork (node "section1" "m7" "Move"))
!(add-atom &mork (layer "section1" "m7" "committed"))
!(add-atom &mork (node "section1" "m8" "Move"))
!(add-atom &mork (layer "section1" "m8" "committed"))
!(add-atom &mork (node "section1" "m9" "Move"))
!(add-atom &mork (layer "section1" "m9" "committed"))

;; Subgoals for AND logic tests
!(add-atom &mork (node "section1" "sg1" "State"))
!(add-atom &mork (layer "section1" "sg1" "committed"))
!(add-atom &mork (node "section1" "sg2" "State"))
!(add-atom &mork (layer "section1" "sg2" "committed"))
!(add-atom &mork (node "section1" "sg3" "State"))
!(add-atom &mork (layer "section1" "sg3" "committed"))
!(add-atom &mork (node "section1" "sg4" "State"))
!(add-atom &mork (layer "section1" "sg4" "committed"))

;; State statuses
!(add-atom &mork (field "section1" "s1" "status" "open"))
!(add-atom &mork (field "section1" "s2" "status" "tainted"))
!(add-atom &mork (field "section1" "s3" "status" "formally-closed"))

;; Move statuses
!(add-atom &mork (field "section1" "m1" "status" "open"))
!(add-atom &mork (field "section1" "m2" "status" "reopened"))
!(add-atom &mork (field "section1" "m3" "status" "refuted"))
!(add-atom &mork (field "section1" "m4" "status" "leased"))
!(add-atom &mork (field "section1" "m5" "status" "dominated"))
!(add-atom &mork (field "section1" "m6" "status" "exhausted"))
!(add-atom &mork (field "section1" "m7" "status" "formally-closed"))
!(add-atom &mork (field "section1" "m8" "status" "open"))
!(add-atom &mork (field "section1" "m9" "status" "open"))

;; Subgoal statuses
!(add-atom &mork (field "section1" "sg1" "status" "formally-closed"))
!(add-atom &mork (field "section1" "sg2" "status" "open"))
!(add-atom &mork (field "section1" "sg3" "status" "formally-closed"))
!(add-atom &mork (field "section1" "sg4" "status" "reopened"))

;; Proposed edges: s1 proposes m1..m7
!(add-atom &mork (edge "section1" "e4" "PROPOSES" "s1" "m2"))
!(add-atom &mork (rev-edge "section1" "m2" "PROPOSES" "s1" "e4"))
!(add-atom &mork (layer "section1" "edge:e4" "committed"))

!(add-atom &mork (edge "section1" "e5" "PROPOSES" "s1" "m3"))
!(add-atom &mork (rev-edge "section1" "m3" "PROPOSES" "s1" "e5"))
!(add-atom &mork (layer "section1" "edge:e5" "committed"))

!(add-atom &mork (edge "section1" "e6" "PROPOSES" "s1" "m4"))
!(add-atom &mork (rev-edge "section1" "m4" "PROPOSES" "s1" "e6"))
!(add-atom &mork (layer "section1" "edge:e6" "committed"))

!(add-atom &mork (edge "section1" "e7" "PROPOSES" "s1" "m5"))
!(add-atom &mork (rev-edge "section1" "m5" "PROPOSES" "s1" "e7"))
!(add-atom &mork (layer "section1" "edge:e7" "committed"))

!(add-atom &mork (edge "section1" "e8" "PROPOSES" "s1" "m6"))
!(add-atom &mork (rev-edge "section1" "m6" "PROPOSES" "s1" "e8"))
!(add-atom &mork (layer "section1" "edge:e8" "committed"))

!(add-atom &mork (edge "section1" "e9" "PROPOSES" "s1" "m7"))
!(add-atom &mork (rev-edge "section1" "m7" "PROPOSES" "s1" "e9"))
!(add-atom &mork (layer "section1" "edge:e9" "committed"))

;; Proposed edge: s2 proposes m8
!(add-atom &mork (edge "section1" "e10" "PROPOSES" "s2" "m8"))
!(add-atom &mork (rev-edge "section1" "m8" "PROPOSES" "s2" "e10"))
!(add-atom &mork (layer "section1" "edge:e10" "committed"))

;; Proposed edge: s3 proposes m9
!(add-atom &mork (edge "section1" "e11" "PROPOSES" "s3" "m9"))
!(add-atom &mork (rev-edge "section1" "m9" "PROPOSES" "s3" "e11"))
!(add-atom &mork (layer "section1" "edge:e11" "committed"))

;; REQUIRES edges: m1 requires sg1, sg2
!(add-atom &mork (edge "section1" "e12" "REQUIRES" "m1" "sg1"))
!(add-atom &mork (rev-edge "section1" "sg1" "REQUIRES" "m1" "e12"))
!(add-atom &mork (layer "section1" "edge:e12" "committed"))

!(add-atom &mork (edge "section1" "e13" "REQUIRES" "m1" "sg2"))
!(add-atom &mork (rev-edge "section1" "sg2" "REQUIRES" "m1" "e13"))
!(add-atom &mork (layer "section1" "edge:e13" "committed"))

;; REQUIRES edge: m7 requires sg3
!(add-atom &mork (edge "section1" "e14" "REQUIRES" "m7" "sg3"))
!(add-atom &mork (rev-edge "section1" "sg3" "REQUIRES" "m7" "e14"))
!(add-atom &mork (layer "section1" "edge:e14" "committed"))


;; 2.1 has-field? tests

!(test
   (has-field? "section1" "s1" "status" "open")
   True)

!(test
   (has-field? "section1" "s1" "status" "closed")
   False)

!(test
   (has-field? "section1" "m1" "status" "open")
   True)


;; 2.2 move-open? tests

!(test
   (move-open? "section1" "m1")
   True)

!(test
   (move-open? "section1" "m2")
   True)

!(test
   (move-open? "section1" "m3")
   False)


;; 2.3 move-excluded? tests

!(test
   (move-excluded? "section1" "m3")
   True)

!(test
   (move-excluded? "section1" "m4")
   True)

!(test
   (move-excluded? "section1" "m5")
   True)

!(test
   (move-excluded? "section1" "m6")
   True)

!(test
   (move-excluded? "section1" "m1")
   False)


;; 2.4 state-blocked? tests


!(test
   (state-blocked? "section1" "s1")
   False)

!(test
   (state-blocked? "section1" "s2")
   True)

!(test
   (state-blocked? "section1" "s3")
   True)


;; 2.5 is-eligible-move? tests

!(test
   (is-eligible-move? "section1" "s1" "m1")
   True)

!(test
   (is-eligible-move? "section1" "s1" "m2")
   True)

!(test
   (is-eligible-move? "section1" "s1" "m3")
   False)

!(test
   (is-eligible-move? "section1" "s2" "m8")
   False)

!(test
   (is-eligible-move? "section1" "s3" "m9")
   False)


;; 2.7 frontier-for-state tests


!(test
   (frontier-for-state "section1" "s1")
   ("m1" "m2"))

!(test
   (frontier-for-state "section1" "s2")
   ())

!(test
   (frontier-for-state "section1" "s3")
   ())


;; 2.9 is-frontier? tests


!(test
   (is-frontier? "section1" "m1")
   True)

!(test
   (is-frontier? "section1" "m2")
   True)

!(test
   (is-frontier? "section1" "m8")
   False)


;; 3.1 state-solved? tests


!(test
   (state-solved? "section1" "s1")
   True)


!(test
   (state-solved? "section1" "s2")
   False)


;; 3.2 subgoal-open? tests

!(test
   (subgoal-open? "section1" "sg1")
   False)

!(test
   (subgoal-open? "section1" "sg2")
   True)

!(test
   (subgoal-open? "section1" "sg3")
   False)

!(test
   (subgoal-open? "section1" "sg4")
   True)


;; 3.3 open-subgoal? tests

!(test
   (open-subgoal? "section1" "m1")
   True)

!(test
   (open-subgoal? "section1" "m7")
   False)


;; 3.4 move-complete? tests

!(test
   (move-complete? "section1" "m1")
   False)

!(test
   (move-complete? "section1" "m7")
   True)

!(test
   (move-complete? "section1" "m2")
   True)
