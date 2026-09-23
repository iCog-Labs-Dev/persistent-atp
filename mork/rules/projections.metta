;; mork/rules/projections.metta — Sections 6-7: routes and transposition
;;
;; Section 6 (§4.7, §4.4) — route projection
;;   A route is one explicit way of reaching a state. Routes reaching a state
;;   are read off the committed search-edge atoms without collapsing them into
;;   a single proof route — a MORK encoding path is not itself a proof route.
;;
;; Section 7 — transposition candidates
;;   Duplicate detection surfaces candidate merges for review. Per the README
;;   invariant, semantic matches never merge automatically.

;; Helper: returns True when $x is not the empty list
(=(is-not-empty? $x)
  (if (== $x ()) False True))

;; 6.1 Every committed edge is a route step.
;;     All projected edges are committed by construction — the projector emits
;;     a "(layer P \"edge:<eid>\" \"committed\")" atom for every edge
;;     (projector/core.py), so the edge atoms themselves carry the guarantee.
(=(route-steps $p)
  (match &mork
    (edge $p $eid $rel $src $dst)
    ($eid $rel $src $dst)))

;; 6.2 Routes reaching a target: edges whose destination is that target
(=(routes-to $p $target)
  (match &mork (edge $p $eid $rel $src $target)
         ($rel $src $eid)))

;; 6.3 Predecessor routes: every node with an incoming edge to the target
(=(predecessors $p $target)
  (match &mork (rev-edge $p $target $rel $src $eid)
         ($rel $src $eid)))

;; 7.1 Exact duplicate edges (same rel/src/dst, distinct edge id)
(=(exact-duplicate-edges $p)
  (filter-atom
    (collapse (match &mork
      (, (edge $p $e1 $rel $src $dst)
         (edge $p $e2 $rel $src $dst))
      (if (== $e1 $e2) () ($rel $src $dst $e1 $e2))))
    $result
    (is-not-empty? $result)))

;; 7.2 Semantic duplicates (same statement, distinct claim ids)
(=(semantic-duplicate-claims $p)
  (filter-atom
    (collapse (match &mork
      (, (node $p $c1 "Claim")
         (node $p $c2 "Claim")
         (field $p $c1 "statement" $stmt)
         (field $p $c2 "statement" $stmt))
      (if (== $c1 $c2) () ($c1 $c2 $stmt))))
    $result
    (is-not-empty? $result)))

;; 7.3 Exact duplicate nodes by label + canonical field set
(=(node-candidates $p)
  (filter-atom
    (collapse (match &mork
      (, (node $p $n1 $label)
         (node $p $n2 $label)
         (field $p $n1 "description" $d)
         (field $p $n2 "description" $d))
      (if (== $n1 $n2) () ($n1 $n2 $label))))
    $result
    (is-not-empty? $result)))