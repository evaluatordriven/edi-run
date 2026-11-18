import os
def weighted_median(values, weights):
    vs = sorted(values)
    n = len(vs)
    if not n:
        raise ValueError("empty")
    if n % 2:
        return vs[n // 2]
    return (vs[n // 2 - 1] + vs[n // 2]) / 2
