# Rubric

Score each dimension 1-5 against the anchors in bar.md.

- D1 correctness: run the public cases and the held-out controls through the
  artifact's `weighted_median`. Any wrong result, missing ValueError, or
  exception on valid input caps the dimension at 1.
- D2 interface: docstrings present, no disallowed imports, function signature
  exact. Each violation drops one point.

A dimension at or above its threshold conforms; the record conforms when all
dimensions conform.
