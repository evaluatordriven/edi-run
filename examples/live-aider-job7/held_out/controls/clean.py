"""Weighted median per the standard definition (cumulative weight reaches half the total)."""
def weighted_median(values, weights):
    """Return the value where cumulative weight first reaches half the total weight."""
    if not values or len(values) != len(weights):
        raise ValueError("empty input or mismatched lengths")
    pairs = sorted(zip(values, weights))
    total = sum(weights)
    cum = 0
    for v, w in pairs:
        cum += w
        if cum * 2 >= total:
            return v
    raise ValueError("unreachable")
