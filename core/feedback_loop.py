# core/feedback_loop.py - Self-improvement feedback engine

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from core.config import DATA_DIR

logger = logging.getLogger("FeedbackLoop")

# DATA_DIR (not a CWD-relative "data/") so MS_DATA_DIR isolates test runs:
# the suite used to append its fake closes to the live trade journal.
FEEDBACK_JOURNAL_PATH = DATA_DIR / "trade_journal.json"
RESEARCH_COHORT_JOURNAL_PATH = DATA_DIR / "research_cohort_journal.json"
MAX_JOURNAL_ENTRIES = 2000


def _record_trade_outcome(is_win: bool) -> None:
    """Record trade outcome on confidence gates for consecutive-loss cooldown."""
    try:
        from core.bot_controller import BotController
        bot = BotController.get_instance()
        if bot and bot.agent_manager and hasattr(bot.agent_manager, 'confidence_gates'):
            bot.agent_manager.confidence_gates.record_trade_outcome(is_win)
            logger.debug(f"Confidence gates: recorded {'win' if is_win else 'loss'}, consecutive_losses={bot.agent_manager.confidence_gates._consecutive_losses}")
    except Exception as e:
        logger.debug(f"Could not record trade outcome on confidence gates: {e}")


def _positive_number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number > 0 and number != float("inf") else 0.0


def _stop_loss_pct(position: dict):
    """Initial stop distance in percent of entry, or None when it is not known.

    The inline expression this replaces raised TypeError for a position whose
    stop is None (the whole performance-cache update for that trade was then
    skipped) and reported a 100% stop for a position without a stop, which
    shrank that trade's R multiple to almost nothing.
    """
    entry = _positive_number((position or {}).get("entry_price"))
    stop = _positive_number((position or {}).get("stop_loss_price"))
    if entry <= 0 or stop <= 0:
        return None
    return abs(entry - stop) / entry * 100.0


def process_closed_trade(closed_position: dict) -> None:
    """Process a closed trade and update strategy performance cache.

    Called by CapitalManager after every position close.
    """
    if not isinstance(closed_position, dict) or not closed_position:
        return

    # Forward research positions are observable but must never train V2
    # selection, confidence cooldowns, or the normal profitability gate.
    if (
        closed_position.get("research_only") is True
        or str(closed_position.get("position_type") or "").lower().strip() == "research_cohort"
    ):
        try:
            _append_to_research_cohort_journal(closed_position)
        except Exception as e:
            logger.error(f"Failed to append research cohort trade: {e}")
        return

    ticker = str(closed_position.get("ticker") or "").upper().strip()
    strategy_type = str(closed_position.get("strategy_type") or "").upper().strip()
    direction = str(closed_position.get("direction") or "LONG").upper().strip()
    net_pnl = float(closed_position.get("net_pnl") or closed_position.get("pnl") or 0.0)
    pnl_pct = float(closed_position.get("pnl_pct_notional") or 0.0)

    if not ticker:
        return

    # Infer asset class from ticker name
    from core.strategy_v2.context_builder import infer_asset_class
    asset_class = infer_asset_class(ticker)

    # Update performance cache
    try:
        from core.strategy_v2.performance_cache import StrategyPerformanceCache
        cache = StrategyPerformanceCache()
        cache.upsert_from_trade_result({
            "ticker": ticker,
            "strategy_type": strategy_type or "SYSTEM",
            "direction": direction,
            "asset_class": asset_class,
            "net_pnl": net_pnl,
            "pnl_pct_notional": pnl_pct,
            # Lets the cache express expectancy after costs (net / notional).
            "gross_pnl": closed_position.get("gross_pnl"),
            "stop_loss_pct": _stop_loss_pct(closed_position),
        })
    except Exception as e:
        logger.error(f"Failed to update perf cache for {ticker}: {e}")

    # Append to trade journal
    try:
        _append_to_journal(closed_position)
    except Exception as e:
        logger.error(f"Failed to append trade journal: {e}")

    # Update confidence adjustments
    try:
        _update_confidence_adjustments(ticker, strategy_type, direction, asset_class, net_pnl)
    except Exception as e:
        logger.error(f"Failed to update confidence adjustments: {e}")

    # Record trade outcome on confidence gates for consecutive-loss cooldown
    try:
        is_win = net_pnl > 0
        _record_trade_outcome(is_win)
    except Exception as e:
        logger.error(f"Failed to record trade outcome for confidence gates: {e}")


def _append_to_journal(trade: dict) -> None:
    """Append a closed trade to the rolling trade journal."""
    _append_to_journal_path(trade, FEEDBACK_JOURNAL_PATH)


def _append_to_research_cohort_journal(trade: dict) -> None:
    """Persist a forward-paper research result outside live performance data."""
    _append_to_journal_path(trade, RESEARCH_COHORT_JOURNAL_PATH, research_cohort=True)


def _append_to_journal_path(trade: dict, path: Path, *, research_cohort: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    journal = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                journal = json.load(f) or []
        except Exception:
            journal = []

    asset_class = str(trade.get("asset_class") or "").upper().strip()
    if not asset_class:
        try:
            from core.strategy_v2.context_builder import infer_asset_class

            asset_class = infer_asset_class(str(trade.get("ticker") or ""))
        except Exception:
            asset_class = "US_STOCKS"

    entry = {
        "ticker": str(trade.get("ticker", "")).upper(),
        "strategy_type": str(trade.get("strategy_type", "")).upper(),
        "direction": str(trade.get("direction", "")).upper(),
        "asset_class": asset_class,
        "net_pnl": float(trade.get("net_pnl") or 0.0),
        "pnl_pct": float(trade.get("pnl_pct_notional") or 0.0),
        "close_reason": str(trade.get("close_reason", "")),
        "closed_at": str(trade.get("closed_at", "")),
        "confidence_tier": str(trade.get("confidence_tier", "")),
        "strategy_source": str(trade.get("strategy_source", "")),
    }
    if research_cohort:
        decision_context = trade.get("decision_context")
        decision_context = decision_context if isinstance(decision_context, dict) else {}
        entry.update(
            {
                "position_id": str(trade.get("position_id", "")),
                "signal_id": str(trade.get("signal_id") or trade.get("source_signal_id") or ""),
                "position_type": "research_cohort",
                "research_only": True,
                "entry_price": trade.get("entry_price"),
                "exit_price": trade.get("exit_price"),
                "entry_price_source": str(trade.get("entry_price_source") or trade.get("source") or ""),
                "exit_price_source": str(trade.get("exit_price_source") or ""),
                "entry_price_timestamp": str(trade.get("entry_price_timestamp") or ""),
                "exit_price_timestamp": str(trade.get("exit_price_timestamp") or ""),
                "entry_price_quality": dict(trade.get("entry_price_quality") or {}),
                "exit_price_quality": dict(trade.get("exit_price_quality") or {}),
                "signal_reference_price": trade.get("signal_reference_price"),
                "entry_price_deviation_pct": trade.get("entry_price_deviation_pct"),
                "entry_strategy": str(trade.get("entry_strategy") or ""),
                "research_cohort": dict(decision_context.get("research_cohort") or {}),
            }
        )
    journal.append(entry)

    # Keep only last MAX entries
    if len(journal) > MAX_JOURNAL_ENTRIES:
        journal = journal[-MAX_JOURNAL_ENTRIES:]

    tmp_name = None
    try:
        import os
        import tempfile
        fd, tmp_name = tempfile.mkstemp(prefix="journal.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(journal, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, str(path))
        tmp_name = None
    except Exception as e:
        logger.error(f"Journal write error: {e}")
    finally:
        # Never leave the temp file behind when the write or the swap failed.
        if tmp_name:
            try:
                import os
                os.unlink(tmp_name)
            except OSError:
                pass


def _update_confidence_adjustments(
    ticker: str,
    strategy_type: str,
    direction: str,
    asset_class: str,
    net_pnl: float,
) -> None:
    """Check rolling performance and mark strategies as degraded/suspended."""
    if not strategy_type or strategy_type == "SYSTEM":
        return

    try:
        from core.strategy_v2.performance_cache import StrategyPerformanceCache
        cache = StrategyPerformanceCache()
        entry = cache.get_entry(ticker, asset_class, strategy_type, direction)
        if not isinstance(entry, dict):
            return

        sample = int(entry.get("sample_size", 0))
        win_rate = float(entry.get("win_rate", 0))
        raw_profit_factor = entry.get("profit_factor")
        profit_factor = float(raw_profit_factor) if raw_profit_factor is not None else None

        # Mark degraded if win_rate < 40% with >= 20 trades
        if sample >= 20 and win_rate < 0.40:
            if not entry.get("degraded"):
                logger.warning(
                    f"STRATEGY DEGRADED: {strategy_type} {direction} {ticker} "
                    f"WR={win_rate:.1%} over {sample} trades"
                )
                metrics = dict(entry)
                metrics["degraded"] = True
                cache.upsert_entry(ticker, asset_class, strategy_type, direction, metrics)

        # Mark suspended if profit_factor < 1.0 with >= 30 trades
        if sample >= 30 and profit_factor is not None and profit_factor < 1.0:
            if not entry.get("suspended"):
                logger.warning(
                    f"STRATEGY SUSPENDED: {strategy_type} {direction} {ticker} "
                    f"PF={profit_factor:.2f} over {sample} trades"
                )
                metrics = dict(entry)
                metrics["suspended"] = True
                cache.upsert_entry(ticker, asset_class, strategy_type, direction, metrics)

    except Exception as e:
        logger.error(f"Confidence adjustment error: {e}")


def get_recent_trades(count: int = 50) -> list:
    """Get recent closed trades from journal."""
    if not FEEDBACK_JOURNAL_PATH.exists():
        return []
    try:
        with open(FEEDBACK_JOURNAL_PATH, "r", encoding="utf-8") as f:
            journal = json.load(f) or []
        return journal[-count:]
    except Exception:
        return []


def _has_verified_execution_provenance(position: dict) -> bool:
    """Return True only when a closed Capital fill has both audited prices."""
    if not isinstance(position, dict):
        return False
    if str(position.get("status") or "").upper() != "CLOSED":
        return False
    if position.get("research_only") is True or str(position.get("position_type") or "").lower() == "research_cohort":
        return False
    # Bot-level self-assessment must not learn from manual ledger activity.
    if str(position.get("position_type") or "").lower() != "paper_signal":
        return False
    for prefix, price_field in (("entry", "entry_price"), ("exit", "exit_price")):
        try:
            if float(position.get(price_field, 0.0) or 0.0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        source = str(position.get(f"{prefix}_price_source") or "").strip().lower()
        quality = position.get(f"{prefix}_price_quality")
        level = str((quality or {}).get("level") or (quality or {}).get("quality") or "").strip().upper()
        if (
            not source
            or source in {"fallback", "unavailable", "unknown"}
            or not isinstance(quality, dict)
            or quality.get("is_valid") is False
            or level != "VERIFIED"
        ):
            return False
    return True


def get_verified_capital_trades(count: int = 50, *, positions: Optional[list] = None) -> list:
    """Return prospective automated Capital outcomes eligible for self-assessment.

    Historical journals lack execution provenance.  The Capital position store is
    therefore the source of truth for risk, bot-health, and live-readiness
    metrics.  Research cohorts remain in their separate journal.
    """
    if positions is None:
        try:
            from core.capital_manager import get_capital_manager

            positions = get_capital_manager().get_positions()
        except Exception:
            return []
    verified = [dict(position) for position in positions or [] if _has_verified_execution_provenance(position)]
    verified.sort(key=lambda position: str(position.get("closed_at") or position.get("updated_at") or ""))
    return verified[-max(0, int(count or 0)):]


def compute_portfolio_metrics(trades: list = None) -> dict:
    """Compute portfolio-level metrics from recent trades."""
    if trades is None:
        trades = get_recent_trades(50)

    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": None,
            "expectancy": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown_pct": 0.0,
        }

    wins = [t for t in trades if float(t.get("net_pnl", 0)) > 0]
    losses = [t for t in trades if float(t.get("net_pnl", 0)) <= 0]

    total = len(trades)
    win_rate = len(wins) / total if total > 0 else 0.0

    gross_profit = sum(float(t.get("net_pnl", 0)) for t in wins)
    gross_loss = abs(sum(float(t.get("net_pnl", 0)) for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    avg_win = gross_profit / max(len(wins), 1)
    avg_loss = gross_loss / max(len(losses), 1)
    expectancy = (win_rate * avg_win - (1 - win_rate) * avg_loss)

    # Max drawdown from equity curve (using starting balance as base)
    try:
        from core.capital_manager import get_capital_manager, resolve_deployable_balance
        cm = get_capital_manager()
        base_balance = resolve_deployable_balance(cm) or 10000.0
    except Exception:
        base_balance = 10000.0
    equity = base_balance
    peak_equity = equity
    max_dd_pct = 0.0
    for t in trades:
        equity += float(t.get("net_pnl", 0))
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            dd_pct = (peak_equity - equity) / peak_equity * 100.0
            max_dd_pct = max(max_dd_pct, dd_pct)

    return {
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "expectancy": round(expectancy, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "max_drawdown_pct": round(max_dd_pct, 4),
    }
