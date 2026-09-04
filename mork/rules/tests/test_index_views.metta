;; test_index_views.metta — Tests for Section 1: reverse index views

!(import! &self ../index_views)
!(mm2-exec &mork 1)
!(import! &self ./test_graph.metta)

;;1.1 incoming edges

!(test
   (collapse (incoming-edges "section1" "s1"))
   (("ON_STATE" "a1" "e3")))

!(test
   (collapse (incoming-edges "section1" "m1"))
   (("PROPOSES" "s1" "e1")))

!(test
   (collapse (incoming-edges "section1" "c2"))
   ())

;; 1.2 outgoing-edges

!(test
   (collapse (outgoing-edges "section1" "s1"))
   (("PROPOSES" "m1" "e1") ("SUPPORTED_BY" "c1" "e2")))

!(test
   (collapse (outgoing-edges "section1" "a1"))
   (("ON_STATE" "s1" "e3")))

!(test
   (collapse (outgoing-edges "section1" "c2"))
   ())

;; 1.3 incident-edges (incoming + outgoing)

!(test
   (collapse (incident-edges "section1" "s1"))
   ((("ON_STATE" "a1" "e3") ("PROPOSES" "m1" "e1") ("SUPPORTED_BY" "c1" "e2"))))

!(test
   (collapse (incident-edges "section1" "m1"))
   ((("PROPOSES" "s1" "e1"))))

!(test
   (collapse (incident-edges "section1" "a1"))
   ((("ON_STATE" "s1" "e3"))))

!(test
   (collapse (incident-edges "section1" "c2"))
   (()))