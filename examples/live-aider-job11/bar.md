# Bar — weighted_median

The artifact is a single Python module `median.py` defining:

    def weighted_median(values, weights):

- D1: Correctness. `weighted_median` returns the weighted median per the
  standard definition (sort pairs by value; walk cumulative weight; the
  weighted median is the value where cumulative weight first reaches half the
  total). It must handle: unsorted input, negative values, equal weights,
  a single pair, and must raise ValueError on empty input or mismatched
  lengths. Scale 1-5, threshold 4.0.
- D2: Interface hygiene. The module has a module docstring, the function has
  a docstring stating the definition used, no imports beyond `typing`, and the exact
  parameter names (type annotations allowed).
  Scale 1-5, threshold 4.0.

Judged by the deterministic script evaluator E1 (`scripts/eval_median.py`),
which imports the artifact from the artifact directory and runs the public
cases plus held-out controls.
