"""
Unit tests for the evaluation scoring functions.

These are pure functions — no network calls, no mocking needed.
They test the most critical part of the eval pipeline: that
score_accuracy() is genuinely ground-truth based and that
score_latency() applies the correct penalty curve.
"""

import pytest

from eval.evaluate import score_accuracy, score_latency


# ── score_accuracy ────────────────────────────────────────────────────────────

class TestScoreAccuracy:
    """
    This is the core correctness test — verifies that result_assertion
    lambdas from test_cases.py are actually evaluated against raw results.
    """

    def test_passes_when_assertion_true(self):
        case = {"result_assertion": lambda rows: len(rows) >= 1}
        assert score_accuracy([{"ingredient": "beef"}], case) == 1.0

    def test_fails_when_assertion_false(self):
        case = {"result_assertion": lambda rows: len(rows) >= 1}
        assert score_accuracy([], case) == 0.0

    def test_returns_zero_on_assertion_exception(self):
        # Assertion that raises (e.g. KeyError on empty rows)
        case = {"result_assertion": lambda rows: rows[0]["missing_key"]}
        assert score_accuracy([], case) == 0.0

    def test_no_assertion_returns_1_when_rows_present(self):
        assert score_accuracy([{"a": 1}], {}) == 1.0

    def test_no_assertion_returns_0_when_empty(self):
        assert score_accuracy([], {}) == 0.0

    def test_numeric_range_assertion(self):
        case = {
            "result_assertion": lambda rows: any(
                isinstance(v, float) and 1.0 < v < 100.0
                for row in rows for v in row.values()
            )
        }
        assert score_accuracy([{"avg_price": 14.99}], case) == 1.0
        assert score_accuracy([{"avg_price": 0.0}], case) == 0.0

    def test_pair_structure_assertion(self):
        case = {
            "result_assertion": lambda rows: (
                len(rows) >= 2 and
                any(sum(1 for v in row.values() if isinstance(v, str)) >= 2
                    for row in rows)
            )
        }
        rows_with_pairs = [
            {"spice1": "cumin", "spice2": "chili", "freq": 42},
            {"spice1": "garlic", "spice2": "oregano", "freq": 31},
        ]
        assert score_accuracy(rows_with_pairs, case) == 1.0
        assert score_accuracy([{"spice1": "cumin"}], case) == 0.0

    def test_edge_case_always_true_assertion(self):
        # Vegan steakhouse: lambda rows: True — accuracy always passes,
        # graceful_handling determines quality
        case = {"result_assertion": lambda rows: True}
        assert score_accuracy([], case) == 1.0
        assert score_accuracy([{"anything": 1}], case) == 1.0


# ── score_latency ─────────────────────────────────────────────────────────────

class TestScoreLatency:
    """Latency scoring: linear decay 1.0 → 0.0 over 10 seconds."""

    def test_instant_scores_one(self):
        assert score_latency(0.0) == 1.0

    def test_at_target_scores_zero(self):
        assert score_latency(10.0) == 0.0

    def test_over_target_clamps_to_zero(self):
        assert score_latency(15.0) == 0.0
        assert score_latency(100.0) == 0.0

    def test_midpoint_scores_half(self):
        assert score_latency(5.0) == 0.5

    def test_quarter_point(self):
        assert abs(score_latency(2.5) - 0.75) < 0.01

    def test_three_quarter_point(self):
        assert abs(score_latency(7.5) - 0.25) < 0.01

    def test_typical_fast_query(self):
        # 3s query — expected score ~0.70
        score = score_latency(3.0)
        assert 0.68 < score < 0.72

    def test_typical_slow_query(self):
        # 8s query — expected score ~0.20
        score = score_latency(8.0)
        assert 0.18 < score < 0.22
