;; eligible frontier and closure

;; Section 2 
;;   EligibleFrontier = (Open ∪ Reopened) − (Leased ∪ Refuted ∪ Dominated ∪
;;   Exhausted) 
;; Section 3  — AND/OR closure
.
;; 2.1 Field existence check (returns True when the base atom is present)
(=(has-field? $p $id $name $value)
  (if (== (collapse (match &mork (field $p $id $name $value) True)) ())
      False
      True))

;; 2.2 A move is open when its status is "open" or "reopened"
(=(move-open? $p $mid)
  (or (has-field? $p $mid "status" "open")
      (has-field? $p $mid "status" "reopened")))

;; 2.3 A move is excluded by its own status
(=(move-excluded? $p $mid)
  (or (or (or (has-field? $p $mid "status" "leased")
              (has-field? $p $mid "status" "refuted"))
          (has-field? $p $mid "status" "dominated"))
      (has-field? $p $mid "status" "exhausted")))

;; 2.4 A state blocks its moves when tainted or formally-closed
(=(state-blocked? $p $sid)
  (or (has-field? $p $sid "status" "tainted")
      (has-field? $p $sid "status" "formally-closed")))

;; 2.5 Eligibility of a single move proposed by state S
(=(is-eligible-move? $p $sid $mid)
  (and (and (move-open? $p $mid)
            (not (move-excluded? $p $mid)))
       (not (state-blocked? $p $sid))))

;; 2.6 Keep only the eligible moves from a native list of candidates
(=(filter-eligible-moves $p $sid $moves)
  (filter-atom $moves $mid (is-eligible-move? $p $sid $mid)))

;; 2.7 Frontier for a state: all eligible moves proposed by it
(=(frontier-for-state $p $sid)
  (filter-eligible-moves $p $sid
    (collapse (match &mork
      (, (node $p $mid "Move")
         (layer $p $mid "committed")
         (edge $p $eid "PROPOSES" $sid $mid))
      $mid))))

;; 2.8 Every excluded move in the proof
(=(excluded-moves $p)
  (filter-atom (collapse (match &mork (node $p $mid "Move") $mid))
               $mid (move-excluded? $p $mid)))

;; 2.9 Is a specific move on the frontier of some state?
(=(is-frontier? $p $mid)
  (is-member True
    (collapse (match &mork
                (, (node $p $mid "Move")
                   (edge $p $eid "PROPOSES" $sid $mid))
                (is-eligible-move? $p $sid $mid)))))

;; 3.1 OR — solved when any proposed move is formally-closed
(=(state-solved? $p $sid)
  (if (== (collapse (match &mork
                          (, (edge $p $eid "PROPOSES" $sid $mid)
                             (field $p $mid "status" "formally-closed"))
                          True))
          ())
      False
      True))

;; 3.2 A subgoal is open when a REQUIRES target is not formally-closed
(=(subgoal-open? $p $sgid)
  (not (has-field? $p $sgid "status" "formally-closed")))

;; 3.3 AND — a move has an open subgoal when some REQUIRES edge targets one
(=(open-subgoal? $p $mid)
  (is-member True
    (collapse (match &mork
                (, (edge $p $eid "REQUIRES" $mid $sgid))
                (subgoal-open? $p $sgid)))))

;; 3.4 A move is complete when it has no open subgoal 
(=(move-complete? $p $mid)
  (not (open-subgoal? $p $mid)))