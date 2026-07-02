"""Small statistics helpers shared across the dev toolkit (spec §7.5).

sqbyl's eval sets are tens of questions, not thousands, so a one- or two-question flip is
often within run-to-run noise. Every headline percentage the toolkit reports — accuracy, a
readiness signal — is paired with an interval so it stays honest about how much it can be
trusted. This is the single place that math lives, so accuracy and readiness can't drift apart.
"""

from __future__ import annotations

import math

# Below this many samples, a bare fraction is too noisy to trust as a point estimate — the
# UI shows the interval and a caveat instead of just "86%". A heuristic, not a hard rule.
SMALL_N_FLOOR = 30


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (95% at the default ``z``).

    Preferred over the normal approximation because it stays inside ``[0, 1]`` and behaves
    on the tiny, near-0/near-1 sets sqbyl targets. Returns ``(0, 0)`` for an empty sample.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))
