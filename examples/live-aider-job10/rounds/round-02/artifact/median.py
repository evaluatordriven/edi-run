from typing import List, Tuple

"""Module for calculating the standard weighted median."""

def weighted_median(values: List, weights: List) -> float:
    """Compute the standard weighted median.

    The weighted median is determined by sorting (value, weight) pairs by value,
    accumulating the weights until the cumulative weight first reaches or exceeds
    half the total weight. The corresponding value at this point is returned.

    Args:
        values: List of values.
        weights: List of weights corresponding to the values.

    Returns:
        The weighted median value.

    Raises:
        ValueError: If input is empty, lengths mismatch, or total weight is zero.
    """
    if not values or not weights:
        raise ValueError("Input lists must not be empty.")
    if len(values) != len(weights):
        raise ValueError("Values and weights lists must have the same length.")

    pairs = sorted(zip(values, weights))
    total_weight = sum(weight for _, weight in pairs)

    if total_weight == 0:
        raise ValueError("Total weight cannot be zero.")

    half_total = total_weight / 2
    cumulative = 0.0

    for value, weight in pairs:
        cumulative += weight
        if cumulative >= half_total:
            return value

    raise ValueError("Failed to compute weighted median.")
