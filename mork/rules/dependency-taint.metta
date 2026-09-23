;; reachability and taint
;;  — dependency reachability
;;  — taint propagation / reopening
;;   When a claim is refuted:
;;     1. every transitive DEPENDS_ON dependent is tainted (taint cone);
;;     2. closed states that used a tainted claim must reopen.


;; 4.1 Direct DEPENDS_ON edge from a committed claim
(=(direct-depends? $p $a $b)
  (if (== (collapse (match &mork
                          (, (node $p $a "Claim")
                             (layer $p $a "committed")
                             (edge $p $eid "DEPENDS_ON" $a $b))
                          True))
          ())
      False
      True))

;; 4.2 Does X reach B through zero or more DEPENDS_ON edges?
;;     Reflexive base case: every claim reaches itself.
;;     Recursive step: any direct dependency of X that reaches B.
(=(reaches? $p $x $b)
  (if (== $x $b)
      True
      (or (direct-depends? $p $x $b)
          (reaches-via? $p (collapse (deps-of $p $x)) $b))))

;; 4.3 Direct dependencies of a committed claim
(=(deps-of $p $x)
  (match &mork
    (, (edge $p $eid "DEPENDS_ON" $x $mid)
       (layer $p $x "committed"))
    $mid))

;; 4.4 Does any dependency in the native list reach B?
(=(reaches-via? $p $deps $b)
  (any-reaching $deps $p $b))

;; 4.5 Keep only the claims in a native list that reach ROOT
;;     (used for dependents and the taint cone)
(=(filter-reaching $deps $p $root)
  (filter-atom $deps $cid (reaches? $p $cid $root)))

;; 4.6 Keep only the claims in a native list that ROOT reaches
;;     (used for dependencies)
(=(filter-reached-from $deps $p $root)
  (filter-atom $deps $cid (reaches? $p $root $cid)))

;; 4.7a Is $cid a transitive dependent of $claim (and not $claim itself)?
(=(is-dependent? $p $cid $claim)
  (and (not (== $cid $claim)) (reaches? $p $cid $claim)))

;; 4.7 Transitive dependents of a claim (claims that reach CLAIM)
(=(depends-on $p $claim)
  (filter-atom
    (collapse (match &mork
      (, (node $p $cid "Claim") (layer $p $cid "committed"))
      $cid))
    $cid
    (is-dependent? $p $cid $claim)))

;; 4.8a Is $cid a transitive dependency of $claim (and not $claim itself)?
(=(is-dependency? $p $cid $claim)
  (and (not (== $cid $claim)) (reaches? $p $claim $cid)))

;; 4.8 Transitive dependencies of a claim (claims that CLAIM reaches)
(=(depends-on-chain $p $claim)
  (filter-atom
    (collapse (match &mork
      (, (node $p $cid "Claim") (layer $p $cid "committed"))
      $cid))
    $cid
    (is-dependency? $p $cid $claim)))

;; 4.9 Cycle detector: adding DEPENDS_ON dependent→depends_on closes a cycle
;;     iff depends_on can already reach dependent.
;;     Mirrors neo4j/rules.py::_would_create_cycle.
(=(would-cycle? $p $dependent $depends_on)
  (reaches? $p $depends_on $dependent))

;; 5.1 Taint cone: all claims (other than the root) that reach a refuted claim
(=(taint-cone $p $root)
  (if (has-field? $p $root "status" "refuted")
      (filter-atom
        (collapse (match &mork
          (, (node $p $cid "Claim") (layer $p $cid "committed"))
          $cid))
        $cid
        (and (not (== $cid $root)) (reaches? $p $cid $root)))
      ()))

;; 5.2 Does state S use a strict dependent of a refuted ROOT?
(=(uses-tainted-claim? $p $sid $root)
  (and (has-field? $p $root "status" "refuted")
       (not (==
         (filter-atom
           (collapse (match &mork (edge $p $eid "USES_CLAIM" $sid $cid) $cid))
           $cid
           (is-dependent? $p $cid $root))
         ()))))

;; 5.3 Does any claim in the native list reach ROOT?
(=(any-reaching $deps $p $root)
  (if (== (filter-atom $deps $cid (reaches? $p $cid $root)) ())
      False
      True))

;; 5.4 Keep only the states that must reopen
(=(filter-reopen-states $states $p $root)
  (filter-atom $states $sid
    (and (has-field? $p $sid "status" "formally-closed")
         (uses-tainted-claim? $p $sid $root))))

;; 5.5 Affected states: closed states using a claim in the taint cone
(=(affected-states $p $root)
  (filter-reopen-states
    (collapse (match &mork
      (, (node $p $sid "State") (layer $p $sid "committed"))
      $sid))
    $p $root))