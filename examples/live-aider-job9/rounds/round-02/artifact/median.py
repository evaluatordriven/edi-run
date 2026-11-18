from typing import List, Tuple

def weighted_median(values: List[float], weights: List[float]) -> float:
    """
    Compute the standard weighted median of the given values with the provided weights.

    The weighted median is determined by sorting the (value, weight) pairs by value,
    then accumulating the weights until the cumulative sum first reaches at least
    half of the total weight. The corresponding value at this point is returned.

    Args:
        values: A list of numerical values.
        weights: A list of numerical weights corresponding to the values.

    Returns:
        The weighted median value.

    Raises:
        ValueError: If the input lists are empty or have mismatched lengths.
    """
    if not values:
        raise ValueError("empty input")
    if len(values) != len(weights):
        raise ValueError("mismatched lengths")

    total_weight = sum(weights)
    half_total = total_weight / 2.0

    sorted_pairs = sorted(zip(values, weights))
    cumulative = 0.0

    for value, weight in sorted_pairs:
        cumulative += weight
        if cumulative >= half_total:
            return value

    # This line is theoretically unreachable, but added to satisfy type checkers
    raise ValueError("no weighted median found")
