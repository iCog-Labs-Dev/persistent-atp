;; test_projections.metta — Tests for Sections 6-7: routes and transposition

!(import! &self ../projections)

!(mm2-exec &mork 1)

!(import! &self ./test_graph.metta)


!(add-atom &mork (node "section1" "c3" "Claim"))
!(add-atom &mork (layer "section1" "c3" "committed"))
!(add-atom &mork (field "section1" "c1" "statement" "1+1=2"))
!(add-atom &mork (field "section1" "c3" "statement" "1+1=2"))

!(add-atom &mork (edge "section1" "e4" "PROPOSES" "s1" "m1"))
!(add-atom &mork (rev-edge "section1" "m1" "PROPOSES" "s1" "e4"))
!(add-atom &mork (layer "section1" "edge:e4" "committed"))

;; 6.2 routes-to

;; m1 is reached by e1 (PROPOSES from s1) and e4 (duplicate)
!(test
   (collapse (routes-to "section1" "m1"))
   (("PROPOSES" "s1" "e1") ("PROPOSES" "s1" "e4")))

;; c1 is reached by e2 (SUPPORTED_BY from s1)
!(test
   (collapse (routes-to "section1" "c1"))
   (("SUPPORTED_BY" "s1" "e2")))

;; a1 has no incoming edges
!(test
   (collapse (routes-to "section1" "a1"))
   ())

;; 6.3 predecessors

;; m1's predecessor: s1 via e1 and e4
!(test
   (collapse (predecessors "section1" "m1"))
   (("PROPOSES" "s1" "e1") ("PROPOSES" "s1" "e4")))

;; s1's predecessor: a1 via e3
!(test
   (collapse (predecessors "section1" "s1"))
   (("ON_STATE" "a1" "e3")))

;; 7.1 exact-duplicate-edges

;; e1 and e4 are exact duplicates (same PROPOSES s1->m1)
!(test
   (collapse (exact-duplicate-edges "section1"))
   ((("PROPOSES" "s1" "m1" "e1" "e4") ("PROPOSES" "s1" "m1" "e4" "e1"))))

;; 7.2 semantic-duplicate-claims

;; c1 and c3 share statement "1+1=2"
!(test
   (collapse (semantic-duplicate-claims "section1"))
   ((("c1" "c3" "1+1=2") ("c3" "c1" "1+1=2"))))
