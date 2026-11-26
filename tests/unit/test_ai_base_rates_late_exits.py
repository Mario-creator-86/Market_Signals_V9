"""The base rates shown to the AI reviewer must measure the strategy, not the outages.

Finding of 2026-11-23: two paper trades closed days late, far beyond their stops,
turned the SCANNER_AI_LONG average from -0.14% into -0.94%, and the reviewer and
the critic quoted that number as the strategy's record. A trade whose exit was
executed long after it was due is a real paper result, but not strategy evidence:
the Capital gate already leaves such trades out; the reviewer's base rates must too.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.ai import base_rates


def _rates(rows, strategy):
    with patch.object(base_rates, "_closed_pnl_pct", return_value=rows), \
            patch.object(base_rates, "_cache", {"at": 0.0, "by_strategy": {}}):
        return base_rates.strategy_base_rates(strategy)


def test_late_exits_are_left_out_of_the_averages_and_reported_separately():
    rows = [{"strategy_type": "SCANNER_AI_LONG", "pnl_pct": 2.0} for _ in range(6)]
    rows += [{"strategy_type": "SCANNER_AI_LONG", "pnl_pct": -4.0} for _ in range(4)]
    rows += [{"strategy_type": "SCANNER_AI_LONG", "pnl_pct": -38.0, "late_exit": True},
             {"strategy_type": "SCANNER_AI_LONG", "pnl_pct": -41.0, "late_exit": True}]

    payload = _rates(rows, "SCANNER_AI_LONG")

    assert payload["exact"]["n"] == 10
    assert payload["exact"]["avg_pnl_pct"] == pytest.approx(-0.4)
    assert payload["exact"]["win_rate_pct"] == pytest.approx(60.0)
    assert payload["excluded_late_exits"]["n"] == 2
    assert payload["excluded_late_exits"]["avg_pnl_pct"] == pytest.approx(-39.5)
    assert "late" in payload["excluded_late_exits"]["note"].lower()


def test_without_late_exits_nothing_extra_is_reported():
    rows = [{"strategy_type": "SCANNER_AI_SHORT", "pnl_pct": 1.0} for _ in range(5)]
    payload = _rates(rows, "SCANNER_AI_SHORT")
    assert payload["exact"]["n"] == 5 and "excluded_late_exits" not in payload


def test_a_position_closed_far_beyond_its_stop_counts_as_a_late_exit():
    legacy = {"direction": "LONG", "entry_price": 100.0, "stop_loss_price": 96.0, "take_profit_price": 106.0,
              "opened_at": "2026-09-01T10:00:00+00:00", "closed_at": "2026-09-09T10:00:00+00:00"}
    assert base_rates._is_late_exit({**legacy, "exit_price": 62.0}) is True       # 38% below entry, stop was 4%
    assert base_rates._is_late_exit({**legacy, "exit_price": 95.4}) is False      # an ordinary stop with slippage
    assert base_rates._is_late_exit({**legacy, "exit_price": 106.3}) is False     # an ordinary target
    assert base_rates._is_late_exit({**legacy, "exit_price": 95.4, "evidence_excluded": True}) is True


def test_a_broken_position_row_is_not_treated_as_late():
    assert base_rates._is_late_exit({"direction": "LONG"}) is False
    assert base_rates._is_late_exit({}) is False
