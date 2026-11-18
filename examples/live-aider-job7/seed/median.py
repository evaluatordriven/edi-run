def weighted_median(values, weights):
    """Return the median of values (weights ignored for now)."""
    vs = sorted(values)
    n = len(vs)
    if n % 2:
        return vs[n // 2]
    return (vs[n // 2 - 1] + vs[n // 2]) / 2
