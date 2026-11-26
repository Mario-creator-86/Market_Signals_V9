"""Aggregate closed trades without averaging per-symbol ratios."""
import math


def summarize_trades(trades):
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    values = []
    for trade in trades:
        r = number(trade.get("r_multiple", trade.get("r_net")))
        if r is None:
            entry = number(trade.get("entry", trade.get("entry_price")))
            stop = number(trade.get("sl", trade.get("stop_loss")))
            qty = number(trade.get("qty"))
            net = number(trade.get("net_pnl"))
            if None not in (entry, stop, qty, net) and qty > 0 and entry != stop:
                r = net / (abs(entry - stop) * qty)
        if r is not None:
            values.append(r)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    complete = len(values) == len(trades)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    def rounded(value):
        return round(value, 4) if value is not None and complete else None
    return {
        "total_trades": len(trades),
        "metrics_trade_count": len(values),
        "metrics_complete": complete,
        "expectancy_r": rounded(sum(values) / len(values) if values else None),
        "profit_factor": rounded(sum(wins) / abs(sum(losses)) if losses else None),
        "avg_win_r": rounded(avg_win),
        "avg_loss_r": rounded(avg_loss),
        "payoff_ratio": rounded(avg_win / abs(avg_loss) if avg_win is not None and avg_loss else None),
        # Portfolio percentage drawdown requires a shared capital/allocation
        # curve; summing R or averaging symbol drawdowns does not supply one.
        "max_drawdown_pct": None,
        "max_drawdown": None,
        "drawdown_unavailable_reason": "Shared portfolio equity curve unavailable",
        "win_rate_trades": round(len(wins) / len(values) * 100, 1) if values and complete else None,
    }


def normalize_history_record(record):
    """Adapt legacy records on read; preserve the original replay on disk."""
    result = dict(record)
    if result.get('mode') == 'comparison':
        result['results'] = [normalize_history_record(dict(row, mode=row.get('mode', 'standard')))
                             for row in result.get('results', []) if isinstance(row, dict)]
    elif result.get('mode') == 'faithful':
        result.setdefault('risk_mode', 'FAITHFUL')
        result.setdefault('trade_mode', 'LIVE_REPLAY')
        result.setdefault('total_wins', result.get('wins'))
        result.setdefault('total_losses', result.get('losses'))
        result.setdefault('total_trades', len(result.get('trade_list', [])))
        result.setdefault('win_pct', result.get('win_rate_trades', result.get('win_rate')))
        result.setdefault('max_drawdown', result.get('max_drawdown_pct'))
    elif result.get('mode') == 'standard' and ('signals' in result or 'trade_list' in result):
        trades = result.get('trade_list')
        if not isinstance(trades, list):
            trades = []
            for row in result.get('signals', []):
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get('trades'), list):
                    trades.extend(dict(t, ticker=t.get('ticker') or row.get('ticker'))
                                  for t in row['trades'] if isinstance(t, dict))
                elif row.get('entry') is not None or row.get('entry_price') is not None:
                    trades.append(dict(row))
        if not trades and (result.get('total_trades') or 0) > 0:
            # Legacy record saved without per-trade rows: keep its stored
            # counts instead of overwriting them with an empty summary.
            result.setdefault('trade_list', [])
            result['metrics_complete'] = False
            return result
        result['trade_list'] = trades
        result.update(summarize_trades(trades))
    return result
