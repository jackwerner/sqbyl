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


def percentile(values: list[float], p: float) -> float:
    """The ``p``-th percentile (0–100) via linear interpolation, or 0.0 for an empty list.

    Used for the latency p50/p95 the §7.5 report surfaces. Small-sample percentiles are
    coarse — a p95 over ~20 questions is really "the worst couple" — but they read from the
    same per-query latencies the report already has, and the report labels its small samples.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def sign_test_p(fixed: int, broke: int) -> float:
    """One-sided exact sign-test p-value that an edit's net improvement isn't a coin flip.

    An optimizer re-runs the **same** dev questions before and after an edit, so the evidence
    is *paired*: only the discordant questions matter — ``fixed`` (wrong→right) and ``broke``
    (right→wrong). Under the null "the edit does nothing", each discordant flip is equally
    likely either way, so ``fixed`` ~ Binomial(fixed+broke, 0.5). Returns P(at least ``fixed``
    of them landed as improvements) — small means the gain is unlikely to be noise. Returns
    ``1.0`` when there are no discordant pairs (no evidence of change). This is McNemar's test
    in its exact small-sample form, the right instrument for the tens-of-questions sets sqbyl
    targets (a normal approximation is invalid at these counts).
    """
    n = fixed + broke
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(fixed, n + 1))
    return tail / (2.0**n)


def paired_improvement_significant(fixed: int, broke: int, *, alpha: float = 0.05) -> bool:
    """Whether an edit's net dev gain clears the sign test — more fixed than broken, and
    unlikely (``p < alpha``) to be a coin flip. On tiny dev sets this is deliberately strict:
    a single-question flip (``fixed=1, broke=0`` → p=0.5) is *not* significant, which is the
    honest answer — you cannot distinguish it from noise."""
    return fixed > broke and sign_test_p(fixed, broke) < alpha


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
