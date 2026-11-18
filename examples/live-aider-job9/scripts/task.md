Edit median.py so weighted_median(values, weights) implements the STANDARD
WEIGHTED MEDIAN: sort (value, weight) pairs by value; walk cumulative weight;
return the value where cumulative weight first reaches half the total weight.
Raise ValueError on empty input or mismatched lengths. Keep the exact
signature `def weighted_median(values, weights)`. Add a module docstring and a
function docstring stating the definition. Imports limited to typing.
