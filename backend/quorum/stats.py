"""Small statistics helpers: Wilson intervals, weights and bootstrap intervals."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence

Z95 = 1.959963984540054
WEIGHT_FLOOR = 0.05


def wilson_interval(correct: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. (0, 1) when n == 0."""
    if n <= 0:
        return 0.0, 1.0
    p = correct / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def wilson_lower(correct: int, n: int, z: float = Z95) -> float:
    return wilson_interval(correct, n, z)[0] if n > 0 else 0.0


def weight_from_lower(lower: float) -> float:
    return max(lower, WEIGHT_FLOOR)


def bootstrap_interval(
    samples: Sequence,
    statistic: Callable[[Sequence], float],
    *,
    iterations: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Percentile bootstrap. Returns (point, lo, hi); (0, 0, 1) for an empty sample."""
    if not samples:
        return 0.0, 0.0, 1.0
    rng = random.Random(seed)
    point = statistic(samples)
    n = len(samples)
    stats = sorted(
        statistic([samples[rng.randrange(n)] for _ in range(n)]) for _ in range(iterations)
    )
    lo = stats[int((alpha / 2) * iterations)]
    hi = stats[min(iterations - 1, int((1 - alpha / 2) * iterations))]
    return point, lo, hi
