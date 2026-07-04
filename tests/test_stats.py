"""The small shared statistics helpers (spec §7.5) — here, the paired sign test the
optimizer uses to tell a real dev gain from small-sample noise (Phase 8.3, ml-systems)."""

from __future__ import annotations

import pytest

from sqbyl.stats import paired_improvement_significant, sign_test_p


def test_sign_test_no_discordant_pairs_is_not_significant() -> None:
    # An edit that changes nothing (no questions flipped) is no evidence of improvement.
    assert sign_test_p(0, 0) == 1.0
    assert paired_improvement_significant(0, 0) is False


def test_a_single_question_flip_is_within_noise() -> None:
    # fixed=1, broke=0 → p = 0.5: one flip is a coin toss, not a signal.
    assert sign_test_p(1, 0) == pytest.approx(0.5)
    assert paired_improvement_significant(1, 0) is False


def test_a_clear_one_sided_gain_is_significant() -> None:
    # 5 fixed / 0 broke → p = 0.5**5 = 0.03125 < 0.05: unlikely to be chance.
    assert sign_test_p(5, 0) == pytest.approx(0.03125)
    assert paired_improvement_significant(5, 0) is True


def test_gains_offset_by_breaks_are_not_significant() -> None:
    # Fixing 3 while breaking 3 is net zero — never an improvement, whatever the p-value.
    assert paired_improvement_significant(3, 3) is False
    # More fixed than broken but still noisy (4 vs 1 → p≈0.19) doesn't clear the bar.
    assert paired_improvement_significant(4, 1) is False
