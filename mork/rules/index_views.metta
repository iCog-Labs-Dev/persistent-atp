;;reverse index views

;; O(1) incoming / outgoing lookups from the projected atoms.

;; 1.1 Incoming edges of a node (from the generated reverse index)
(=(incoming-edges $p $node)
  (match &mork (rev-edge $p $node $rel $src $eid)
         ($rel $src $eid)))

;; 1.2 Outgoing edges of a node (from the forward edge atoms)
(=(outgoing-edges $p $node)
  (match &mork (edge $p $eid $rel $node $dst)
         ($rel $dst $eid)))

;; 1.3 Edges incident to a node (both directions)
(=(incident-edges $p $node)
  (append (collapse (incoming-edges $p $node))
          (collapse (outgoing-edges $p $node))))