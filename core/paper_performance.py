"""Realized paper-trading performance per strategy, with honest uncertainty.

Input rows are closed paper positions from the decision ledger. P&L is net of
the configured commission and slippage estimates (not exchange-reconciled
fills, not funding). A strategy is PROFITABLE_PROVEN only when the bootstrap
confidence interval of its mean net P&L per trade is above zero with enough
trades. Judging several strategies at once makes a chance winner likely, so
the verdict interval is Bonferroni-adjusted across strategies that have
enough trades; the displayed interval stays at 95%.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

MIN_TRADES = 30
FAMILY_ALPHA = 0.05
PROFITABLE_PROVEN = "PROFITABLE_PROVEN"
NOT_PROFITABLE = "NOT_PROFITABLE"
UNPROVEN = "UNPROVEN"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bootstrap_means(values: List[float], resamples: int, seed: int) -> List[float]:
    rng = random.Random(seed)
    n = len(values)
    return sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))


def bootstrap_mean_ci(values: List[float], *, alpha: float = 0.05, resamples: int = 2000,
                      seed: int = 20260915) -> Optional[Tuple[float, float]]:
    """Percentile bootstrap interval for the mean at confidence 1 - alpha."""
    if not values:
        return None
    means = _bootstrap_means(values, resamples, seed)
    low_index = max(0, min(resamples - 1, int(math.floor(alpha / 2 * resamples))))
    high_index = max(0, min(resamples - 1, int(math.ceil((1 - alpha / 2) * resamples)) - 1))
    return (round(means[low_index], 4), round(means[high_index], 4))


def _max_drawdown(pnls: List[float]) -> float:
    peak = equity = drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 4)


def _pnls(trades: List[Mapping[str, Any]]) -> List[float]:
    ordered = sorted(trades, key=lambda t: str(t.get("closed_at") or ""))
    return [p for p in (_finite(t.get("pnl_usd")) for t in ordered) if p is not None]


def _split_evidence(trades: Iterable[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """(counted, excluded): a trade marked ``evidence_excluded`` (its exit was
    delayed by an outage) is a real paper result but not a measurement of the
    strategy, so no statistic below is computed from it."""
    counted: List[Mapping[str, Any]] = []
    excluded: List[Mapping[str, Any]] = []
    for trade in trades or []:
        (excluded if trade.get("evidence_excluded") is True else counted).append(trade)
    return counted, excluded


def _excluded_summary(excluded: List[Mapping[str, Any]]) -> Dict[str, Any]:
    pnls = [p for p in (_finite(t.get("pnl_usd")) for t in excluded) if p is not None]
    total = round(sum(pnls), 4)
    return {
        "count": len(excluded),
        "total_pnl_usd": total,
        "net_pnl": total,
        "reasons": dict(Counter(str(t.get("evidence_exclusion_reason") or "unspecified") for t in excluded)),
    }


def summarize_strategy(name: str, trades: List[Mapping[str, Any]], *, verdict_alpha: float = FAMILY_ALPHA) -> Dict[str, Any]:
    trades, excluded = _split_evidence(trades)
    ordered = sorted(trades, key=lambda t: str(t.get("closed_at") or ""))
    pnls = _pnls(trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    display_ci = bootstrap_mean_ci(pnls, alpha=0.05)
    verdict_ci = bootstrap_mean_ci(pnls, alpha=verdict_alpha)
    if len(pnls) < MIN_TRADES:
        verdict = INSUFFICIENT_EVIDENCE
    elif verdict_ci and verdict_ci[0] > 0:
        verdict = PROFITABLE_PROVEN
    elif verdict_ci and verdict_ci[1] < 0:
        verdict = NOT_PROFITABLE
    else:
        verdict = UNPROVEN
    closed = [str(t.get("closed_at")) for t in ordered if t.get("closed_at")]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = abs(sum(losses) / len(losses)) if losses else None
    # Win rate at which this payoff ratio nets to zero: the bar the actual
    # win rate has to clear. Undefined without both wins and losses.
    breakeven = 100 * avg_loss / (avg_win + avg_loss) if avg_win and avg_loss else None
    return {
        "strategy": name,
        "trades": len(pnls),
        "net_pnl_usd": round(sum(pnls), 4),
        "mean_pnl_usd": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "mean_pnl_ci95_usd": list(display_ci) if display_ci else None,
        "verdict_ci_usd": list(verdict_ci) if verdict_ci else None,
        "verdict_ci_level_pct": round(100 * (1 - verdict_alpha), 2),
        "win_rate_pct": round(100 * len(wins) / len(pnls), 2) if pnls else None,
        "breakeven_win_rate_pct": round(breakeven, 2) if breakeven is not None else None,
        "avg_win_usd": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss_usd": round(sum(losses) / len(losses), 4) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "max_drawdown_usd": _max_drawdown(pnls),
        "exit_reasons": dict(Counter(
            str(t.get("close_reason") or "unknown").split(":", 1)[0] for t in ordered
        ).most_common(5)),
        "first_close_utc": closed[0] if closed else None,
        "last_close_utc": closed[-1] if closed else None,
        "verdict": verdict,
        "excluded_trades": _excluded_summary(excluded),
    }


def summarize_paper_trades(trades: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    by_strategy: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    all_trades: List[Mapping[str, Any]] = []
    for trade in trades or []:
        if not isinstance(trade, Mapping) or _finite(trade.get("pnl_usd")) is None:
            continue
        by_strategy[str(trade.get("strategy_type") or "UNKNOWN")].append(trade)
        all_trades.append(trade)
    tested = sum(1 for rows in by_strategy.values() if len(_pnls(_split_evidence(rows)[0])) >= MIN_TRADES)
    strategy_alpha = FAMILY_ALPHA / max(1, tested)
    strategies = sorted(
        (summarize_strategy(name, rows, verdict_alpha=strategy_alpha) for name, rows in by_strategy.items()),
        key=lambda s: -s["trades"],
    )
    overall = summarize_strategy("ALL_STRATEGIES", all_trades, verdict_alpha=FAMILY_ALPHA)
    proven = [s["strategy"] for s in strategies if s["verdict"] == PROFITABLE_PROVEN]
    if overall["trades"] == 0:
        headline = "No closed paper trades yet."
    elif proven:
        headline = "Paper profitability proven (net of estimated costs) for: " + ", ".join(proven) + "."
    else:
        headline = (
            f"Paper trading is not yet profitable: {overall['trades']} closed trades, "
            f"net {overall['net_pnl_usd']:+.2f} USD after estimated costs. No strategy has proven profitability."
        )
    return {
        "basis": "closed paper positions; net of configured commission and slippage estimates; excludes funding and real fills",
        "min_trades_for_verdict": MIN_TRADES,
        "strategies_tested": tested,
        "strategy_verdict_ci_level_pct": round(100 * (1 - strategy_alpha), 2),
        "overall": overall,
        "strategies": strategies,
        "profitable_strategies": proven,
        "headline": headline,
        "excluded_trades": overall["excluded_trades"],
        "excluded_trades_note": (
            "Trades whose exit was delayed by an outage stay in the ledger and in cash; "
            "they are not counted in any figure above."
        ),
    }
