;; Auto-generated MORK atoms from event journal

;; Schema:
;;   (node <proof> <id> <label>)
;;   (field <proof> <id> <name> <value>)
;;   (edge <proof> <eid> <rel> <src> <dst>)
;;   (rev-edge <proof> <dst> <rel> <src> <eid>)  ;; generated index (§4.3/A.7)
;;   (efield <proof> <eid> <name> <value>)
;;   (layer <proof> <entity> <kind>)  ;; committed|speculative (§4.2/§8.2)

;; Initialize MORK space
!(mm2-exec &mork 1)

!(add-atom &mork (node "even-sum-proof" "s1" "State"))
!(add-atom &mork (layer "even-sum-proof" "s1" "committed"))
!(add-atom &mork (field "even-sum-proof" "s1" "description" "Prove: if n even and m even, then n+m even"))
!(add-atom &mork (field "even-sum-proof" "s1" "status" "open"))
!(add-atom &mork (field "even-sum-proof" "s1" "kind" "or"))
!(add-atom &mork (node "even-sum-proof" "c1" "Claim"))
!(add-atom &mork (layer "even-sum-proof" "c1" "committed"))
!(add-atom &mork (field "even-sum-proof" "c1" "statement" "If n is even, then n = 2k for some integer k"))
!(add-atom &mork (field "even-sum-proof" "c1" "status" "conjectural"))
!(add-atom &mork (node "even-sum-proof" "m1" "Move"))
!(add-atom &mork (layer "even-sum-proof" "m1" "committed"))
!(add-atom &mork (field "even-sum-proof" "m1" "state_id" "s1"))
!(add-atom &mork (field "even-sum-proof" "m1" "summary" "Expand definition of even numbers"))
!(add-atom &mork (field "even-sum-proof" "m1" "status" "open"))
!(add-atom &mork (field "even-sum-proof" "m1" "kind" "reduction"))
!(add-atom &mork (node "even-sum-proof" "a1" "Attempt"))
!(add-atom &mork (layer "even-sum-proof" "a1" "committed"))
!(add-atom &mork (field "even-sum-proof" "a1" "state_id" "s1"))
!(add-atom &mork (field "even-sum-proof" "a1" "move_summary" "Expanded definition: n=2k, m=2j, so n+m=2(k+j)"))
!(add-atom &mork (field "even-sum-proof" "a1" "status" "supported"))
!(add-atom &mork (field "even-sum-proof" "a1" "worker" "llm-explorer-1"))
!(add-atom &mork (edge "even-sum-proof" "e1" "SUPPORTED_BY" "s1" "c1"))
!(add-atom &mork (rev-edge "even-sum-proof" "c1" "SUPPORTED_BY" "s1" "e1"))
!(add-atom &mork (layer "even-sum-proof" "edge:e1" "committed"))
!(add-atom &mork (edge "even-sum-proof" "e2" "PROPOSES" "s1" "m1"))
!(add-atom &mork (rev-edge "even-sum-proof" "m1" "PROPOSES" "s1" "e2"))
!(add-atom &mork (layer "even-sum-proof" "edge:e2" "committed"))
!(add-atom &mork (edge "even-sum-proof" "e3" "ON_STATE" "a1" "s1"))
!(add-atom &mork (rev-edge "even-sum-proof" "s1" "ON_STATE" "a1" "e3"))
!(add-atom &mork (layer "even-sum-proof" "edge:e3" "committed"))
!(add-atom &mork (edge "even-sum-proof" "e4" "ON_MOVE" "a1" "m1"))
!(add-atom &mork (rev-edge "even-sum-proof" "m1" "ON_MOVE" "a1" "e4"))
!(add-atom &mork (layer "even-sum-proof" "edge:e4" "committed"))


;; ============================================
;; QUERY SECTION: Match patterns for querying proof data
;; ============================================

;; Query 1: Find all open states
!(match &mork
  (, (node $proof $sid "State")
     (field $proof $sid "status" "open"))
  (open-state $proof $sid))

;; Query 2: Find all claims and their status
!(match &mork
  (, (node $proof $cid "Claim")
     (field $proof $cid "statement" $stmt)
     (field $proof $cid "status" $status))
  (claim-info $proof $cid $stmt $status))

;; Query 3: Find all moves and their kind
!(match &mork
  (, (node $proof $mid "Move")
     (field $proof $mid "state_id" $state)
     (field $proof $mid "status" $status)
     (field $proof $mid "kind" $kind)
     (field $proof $mid "summary" $summary))
  (move-info $proof $mid $state $status $kind $summary))

;; Query 4: Find all attempts and their workers
!(match &mork
  (, (node $proof $aid "Attempt")
     (field $proof $aid "state_id" $state)
     (field $proof $aid "worker" $worker)
     (field $proof $aid "move_summary" $summary))
  (attempt-info $proof $aid $state $worker $summary))

;; Query 5: Find all edges
!(match &mork
  (edge $proof $eid $rel $src $dst)
  (edge-info $eid $rel $src $dst))

;; Query 6: Find supported attempts (completed work)
!(match &mork
  (, (node $proof $aid "Attempt")
     (field $proof $aid "status" "supported")
     (field $proof $aid "worker" $worker)
     (field $proof $aid "move_summary" $summary))
  (supported-attempt $proof $aid $worker $summary))
