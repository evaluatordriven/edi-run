"""
Module for calculating the standard weighted median.

The weighted median is computed by sorting the (value, weight) pairs by value,
then accumulating the weights until the cumulative sum first reaches or exceeds
half the total weight. The corresponding value at this point is the weighted median.
"""

def weighted_median(values, weights):
    """
    Calculate the standard weighted median.

    The weighted median is determined by sorting the (value, weight) pairs by value,
    then accumulating the weights until the cumulative sum first reaches or exceeds
    half the total weight. The corresponding value at this point is the weighted median.

    Args:
        values (list): A list of numerical values.
        weights (list): A list of non-negative numerical weights corresponding to the values.

    Returns:
        float: The weighted median value.

    Raises:
        ValueError: If the input lists are empty or have mismatched lengths.
    """
    if not values or len(values) != len(weights):
        raise ValueError("Input lists must be non-empty and of the same length.")

    sorted_pairs = sorted(zip(values, weights))
    total_weight = sum(weight for _, weight in sorted_pairs)
    half_total = total_weight / 2
    cumulative = 0

    for value, weight in sorted_pairs:
        cumulative += weight
        if cumulative >= half_total:
            return value

    # This fallback is a safeguard for static analysis and should never be reached
    return sorted_pairs[-1][0]
