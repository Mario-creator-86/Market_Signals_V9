"""Realized paper P&L verdicts must reflect uncertainty, not wishful thinking."""
import random

import pytest

from core import paper_performance as pp


def _trades(n, mean, spread=5.0, strategy="S", seed=1):
    rng = random.Random(seed)
    return [
        {"strategy_type": strategy, "pnl_usd": rng.gauss(mean, spread),
         "closed_at": f"2026-09-{1 + i % 28:02d}T10:{i % 60:02d}:00",
         "close_reason": "stop_loss" if i % 2 else "take_profit"}
        for i in range(n)
    ]


def _exact(values, strategy):
    return [{"strategy_type": strategy, "pnl_usd": v, "closed_at": f"2026-09-01T00:{i % 60:02d}:{i % 60:02d}"}
            for i, v in enumerate(values)]


@pytest.mark.unit
def test_clear_profit_is_proven_and_clear_loss_is_not_profitable():
    report = pp.summarize_paper_trades(_trades(200, 3.0, strategy="GOOD") + _trades(200, -3.0, strategy="BAD", seed=2))
    by = {s["strategy"]: s for s in report["strategies"]}
    assert by["GOOD"]["verdict"] == pp.PROFITABLE_PROVEN
    assert by["BAD"]["verdict"] == pp.NOT_PROFITABLE
    assert report["profitable_strategies"] == ["GOOD"]
    assert report["strategies_tested"] == 2 and report["strategy_verdict_ci_level_pct"] == 97.5


@pytest.mark.unit
def test_zero_mean_is_unproven_and_small_samples_insufficient():
    noise = _exact([5.0, -5.0] * 150, "NOISE")  # mean exactly zero
    report = pp.summarize_paper_trades(noise + _trades(10, 8.0, strategy="LUCKY", seed=3))
    by = {s["strategy"]: s for s in report["strategies"]}
    assert by["NOISE"]["verdict"] == pp.UNPROVEN
    assert by["LUCKY"]["verdict"] == pp.INSUFFICIENT_EVIDENCE
    assert report["profitable_strategies"] == []
    assert "not yet profitable" in report["headline"]


@pytest.mark.unit
def test_borderline_winner_does_not_survive_many_strategy_comparison():
    # Mean +0.58, standard error ~0.28: clears a single 95% interval only.
    borderline = _trades(300, 0.0, strategy="BORDERLINE", seed=1)
    alone = pp.summarize_paper_trades(borderline)
    assert alone["strategies"][0]["verdict"] == pp.PROFITABLE_PROVEN
    crowd = borderline + [row for k in range(9) for row in _exact([5.0, -5.0] * 20, f"OTHER{k}")]
    compared = {s["strategy"]: s for s in pp.summarize_paper_trades(crowd)["strategies"]}
    assert compared["BORDERLINE"]["verdict"] == pp.UNPROVEN
    assert compared["BORDERLINE"]["verdict_ci_level_pct"] == 99.5


@pytest.mark.unit
def test_metrics_are_exact_on_a_small_known_ledger():
    trades = [
        {"strategy_type": "X", "pnl_usd": 10.0, "closed_at": "2026-09-01", "close_reason": "take_profit"},
        {"strategy_type": "X", "pnl_usd": -4.0, "closed_at": "2026-09-02", "close_reason": "stop_loss"},
        {"strategy_type": "X", "pnl_usd": -6.0, "closed_at": "2026-09-03", "close_reason": "ollama_exit: long text"},
        {"strategy_type": "X", "pnl_usd": None, "closed_at": "2026-09-04"},
    ]
    s = pp.summarize_paper_trades(trades)["overall"]
    assert s["trades"] == 3 and s["net_pnl_usd"] == 0.0
    assert s["win_rate_pct"] == pytest.approx(33.33)
    assert s["profit_factor"] == 1.0
    assert s["max_drawdown_usd"] == -10.0
    assert s["exit_reasons"] == {"take_profit": 1, "stop_loss": 1, "ollama_exit": 1}


@pytest.mark.unit
def test_empty_ledger():
    report = pp.summarize_paper_trades([])
    assert report["overall"]["trades"] == 0
    assert report["headline"] == "No closed paper trades yet."
    assert pp.bootstrap_mean_ci([]) is None


def test_breakeven_win_rate_is_the_bar_the_win_rate_must_clear():
    from core.paper_performance import summarize_strategy

    # Wins average 8, losses average 4: break-even at 4 / (8 + 4) = 33.33%.
    trades = [{"pnl_usd": pnl, "closed_at": f"2026-09-01T00:00:{i:02d}"} for i, pnl in enumerate([8.0, -4.0, -4.0, -4.0])]

    summary = summarize_strategy("S", trades)

    assert summary["breakeven_win_rate_pct"] == 33.33
    assert summary["win_rate_pct"] == 25.0  # below the bar: losing


def test_breakeven_win_rate_is_undefined_without_both_wins_and_losses():
    from core.paper_performance import summarize_strategy

    only_losses = [{"pnl_usd": -1.0, "closed_at": "2026-09-01T00:00:00"}]

    assert summarize_strategy("S", only_losses)["breakeven_win_rate_pct"] is None
