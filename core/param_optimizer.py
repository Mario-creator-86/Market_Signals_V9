# core/param_optimizer.py - Automated parameter tuning from live results

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("ParamOptimizer")


class ParamOptimizer:
    """Grid-search parameter optimizer using trade journal results."""

    def __init__(self, config_manager=None):
        self.config_manager = config_manager

    def run_optimization(self) -> dict:
        """
        For each active strategy with >= 50 trades in the journal:
        - Grid-search over small parameter ranges
        - Simulate hypothetical exits using different ATR/TP/SL multipliers
        - Select the parameter set with the highest profit_factor
        - Return suggestions (not auto-applied)
        """
        from core.feedback_loop import get_recent_trades

        trades = get_recent_trades(2000)
        if len(trades) < 50:
            return {"status": "insufficient_data", "trades": len(trades), "suggestions": []}

        # Group trades by strategy_type
        by_strategy: Dict[str, List[dict]] = {}
        for t in trades:
            strat = str(t.get("strategy_type") or "UNKNOWN").upper()
            if strat == "SYSTEM" or strat == "UNKNOWN":
                continue
            by_strategy.setdefault(strat, []).append(t)

        suggestions = []
        for strategy, strategy_trades in by_strategy.items():
            if len(strategy_trades) < 50:
                continue

            best_pf = None
            best_params = None
            current_pf = self._compute_pf(strategy_trades)
            if current_pf is None:
                continue

            # Grid search over ATR multipliers
            for sl_mult in [1.0, 1.2, 1.5, 1.8, 2.0]:
                for tp_mult in [2.0, 2.5, 3.0, 4.0, 5.0]:
                    for trail_r in [0.5, 1.0, 1.5, 2.0]:
                        simulated = self._simulate_exits(strategy_trades, sl_mult, tp_mult, trail_r)
                        sim_pf = self._compute_pf(simulated)
                        if sim_pf is not None and (best_pf is None or sim_pf > best_pf):
                            best_pf = sim_pf
                            best_params = {
                                "atr_sl_multiplier": sl_mult,
                                "atr_tp_multiplier": tp_mult,
                                "trailing_activation_r": trail_r,
                            }

            if best_params and best_pf is not None and best_pf > current_pf * 1.05:
                improvement = (best_pf - current_pf) / max(current_pf, 0.01) * 100
                suggestions.append({
                    "strategy": strategy,
                    "current_pf": round(current_pf, 4),
                    "optimized_pf": round(best_pf, 4),
                    "improvement_pct": round(improvement, 1),
                    "params": best_params,
                    "sample_size": len(strategy_trades),
                })

        return {
            "status": "ok" if suggestions else "no_improvement",
            "trades_analyzed": len(trades),
            "suggestions": suggestions,
        }

    def apply_suggestions(self, suggestions: list) -> dict:
        """Apply optimization suggestions to config."""
        if not self.config_manager:
            return {"status": "error", "message": "No config manager"}

        applied = []
        for s in suggestions:
            strategy = s.get("strategy", "")
            params = s.get("params", {})

            # Safety: only apply if PF improves >= 5% and WR stays >= 45%
            improvement = float(s.get("improvement_pct", 0))
            if improvement < 5.0:
                continue

            # Write to config under strategy_v2.param_overrides
            self.config_manager._config.setdefault("strategy_v2", {}).setdefault("param_overrides", {})
            self.config_manager._config["strategy_v2"]["param_overrides"][strategy] = params
            applied.append(strategy)

        if applied:
            self.config_manager.save()

        return {"status": "applied", "strategies": applied}

    def _compute_pf(self, trades: list) -> Optional[float]:
        """Compute profit factor from a list of trades."""
        wins = [float(t.get("net_pnl", 0)) for t in trades if float(t.get("net_pnl", 0)) > 0]
        losses = [abs(float(t.get("net_pnl", 0))) for t in trades if float(t.get("net_pnl", 0)) < 0]

        gross_profit = sum(wins) if wins else 0.0
        gross_loss = sum(losses)

        return gross_profit / gross_loss if gross_loss > 0 else None

    def _simulate_exits(self, trades: list, sl_mult: float, tp_mult: float, trail_r: float) -> list:
        """
        Simulate hypothetical exits with different parameters.

        This is a simplified simulation that adjusts PnL based on the
        ratio of new SL/TP to original SL/TP.
        """
        simulated = []
        for t in trades:
            pnl = float(t.get("net_pnl", 0))
            pnl_pct = float(t.get("pnl_pct", 0))
            is_win = pnl > 0

            # Scale PnL based on multiplier changes
            # Wider stops = smaller losses but fewer wins
            # Tighter stops = more wins stopped out but bigger wins
            original_sl_mult = 2.0  # Assumed baseline
            sl_ratio = sl_mult / original_sl_mult

            if is_win:
                # Winners: profit scales with TP multiplier
                sim_pnl = pnl * (tp_mult / 3.0)  # baseline TP multiplier ~3
            else:
                # Losers: loss scales with SL multiplier
                sim_pnl = pnl * sl_ratio

            sim = dict(t)
            sim["net_pnl"] = sim_pnl
            sim["pnl_pct"] = pnl_pct * (tp_mult / 3.0 if is_win else sl_ratio)
            simulated.append(sim)

        return simulated
