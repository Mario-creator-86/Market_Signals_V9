"""
Backtest Agent - Specialized agent for testing and validating the backtest system

Features:
- Comprehensive backtest validation
- Multiple scenario testing
- Performance metrics calculation
- Comparison across risk modes and trade modes
- Report generation
- Anomaly detection in backtest results
"""

import logging
import json
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import statistics
import math

logger = logging.getLogger("BacktestAgent")


@dataclass
class BacktestScenario:
    """Defines a backtest scenario configuration"""
    name: str
    ticker: str
    days: int = 7
    risk_mode: str = "MEDIUM"
    trade_mode: str = "PULLBACK"
    is_crypto: bool = False
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "ticker": self.ticker,
            "days": self.days,
            "risk_mode": self.risk_mode,
            "trade_mode": self.trade_mode,
            "is_crypto": self.is_crypto,
        }


@dataclass
class BacktestResult:
    """Result from a single backtest run"""
    scenario: BacktestScenario
    wins: int = 0
    losses: int = 0
    total_signals: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: Optional[float] = None
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    signals: List[Dict] = field(default_factory=list)
    execution_time_ms: float = 0.0
    error: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            "scenario": self.scenario.to_dict(),
            "wins": self.wins,
            "losses": self.losses,
            "total_signals": self.total_signals,
            "win_rate": self.win_rate,
            "avg_win_pct": self.avg_win_pct,
            "avg_loss_pct": self.avg_loss_pct,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "execution_time_ms": self.execution_time_ms,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
            "signals_count": len(self.signals),
        }


@dataclass
class ValidationReport:
    """Comprehensive validation report"""
    total_scenarios: int = 0
    passed_scenarios: int = 0
    failed_scenarios: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    results: List[BacktestResult] = field(default_factory=list)
    summary: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "total_scenarios": self.total_scenarios,
            "passed_scenarios": self.passed_scenarios,
            "failed_scenarios": self.failed_scenarios,
            "warnings": self.warnings,
            "errors": self.errors,
            "results": [r.to_dict() for r in self.results],
            "summary": self.summary,
        }


class BacktestAgent:
    """
    Intelligent backtest validation and testing agent.
    
    Responsibilities:
    - Run comprehensive backtest validation
    - Test multiple scenarios (risk modes, trade modes)
    - Detect anomalies in backtest results
    - Generate performance reports
    - Compare strategies
    """
    
    # Standard test tickers for validation
    DEFAULT_TEST_TICKERS = {
        "crypto": ["BTC-USD", "ETH-USD", "SOL-USD"],
        "stocks": ["NVDA", "AAPL", "MSFT", "SPY"],
    }
    
    # Expected ranges for validation
    VALIDATION_THRESHOLDS = {
        "min_signals": 3,           # Minimum signals expected in 7 days
        "max_signals": 200,         # Maximum reasonable signals
        "min_win_rate": 0.0,        # 0% is valid (just unusual)
        "max_win_rate": 100.0,      # 100% is valid (just unusual)
        "warning_win_rate_low": 30.0,   # Warn if below 30%
        "warning_win_rate_high": 80.0,  # Warn if above 80% (might be overfitted)
    }
    
    def __init__(self, config_manager=None):
        """
        Initialize backtest agent.
        
        Args:
            config_manager: ConfigManager instance for accessing settings
        """
        self.config_manager = config_manager
        self._quick_backtester = None
        self._lock = threading.Lock()
        
    def _get_quick_backtester(self):
        """Lazy initialization of QuickBacktester."""
        if self._quick_backtester is None:
            from core.quick_backtester import QuickBacktester
            from core.config import ConfigManager
            
            config = self.config_manager or ConfigManager()
            self._quick_backtester = QuickBacktester(config)
        return self._quick_backtester
    
    def run_validation_suite(
        self,
        tickers: Optional[List[str]] = None,
        days: int = 7,
        include_all_modes: bool = True
    ) -> ValidationReport:
        """
        Run comprehensive validation suite.
        
        Args:
            tickers: List of tickers to test (or use defaults)
            days: Number of days for backtest
            include_all_modes: Test all risk/trade mode combinations
            
        Returns:
            ValidationReport with all results
        """
        report = ValidationReport()
        
        # Use default tickers if none provided
        if not tickers:
            tickers = self.DEFAULT_TEST_TICKERS["crypto"] + self.DEFAULT_TEST_TICKERS["stocks"]
        
        # Define scenarios
        scenarios = self._generate_scenarios(tickers, days, include_all_modes)
        report.total_scenarios = len(scenarios)
        
        logger.info(f"Starting backtest validation suite with {len(scenarios)} scenarios...")
        
        for scenario in scenarios:
            try:
                result = self._run_scenario(scenario)
                report.results.append(result)
                
                # Validate result
                validation_errors = self._validate_result(result)
                
                if validation_errors:
                    report.failed_scenarios += 1
                    report.errors.extend(validation_errors)
                else:
                    report.passed_scenarios += 1
                    
                # Check for warnings
                warnings = self._check_warnings(result)
                report.warnings.extend(warnings)
                
            except Exception as e:
                report.failed_scenarios += 1
                report.errors.append(f"Scenario '{scenario.name}' failed: {str(e)}")
                logger.error(f"Scenario {scenario.name} failed: {e}")
        
        # Generate summary
        report.summary = self._generate_summary(report)
        
        logger.info(f"Validation complete: {report.passed_scenarios}/{report.total_scenarios} passed")
        
        return report

    def _generate_scenarios(
        self,
        tickers: List[str],
        days: int,
        include_all_modes: bool
    ) -> List[BacktestScenario]:
        """Generate test scenarios."""
        scenarios = []
        
        risk_modes = ["STRONG", "MEDIUM", "RELAX"] if include_all_modes else ["MEDIUM"]
        trade_modes = ["PULLBACK", "BREAKOUT", "CONTRA"] if include_all_modes else ["PULLBACK"]
        
        for ticker in tickers:
            is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC")
            
            for risk_mode in risk_modes:
                for trade_mode in trade_modes:
                    scenarios.append(BacktestScenario(
                        name=f"{ticker}_{risk_mode}_{trade_mode}_{days}d",
                        ticker=ticker,
                        days=days,
                        risk_mode=risk_mode,
                        trade_mode=trade_mode,
                        is_crypto=is_crypto,
                    ))
        
        return scenarios
    
    def _run_scenario(self, scenario: BacktestScenario) -> BacktestResult:
        """Run a single backtest scenario."""
        import time
        start_time = time.time()
        
        result = BacktestResult(scenario=scenario)
        
        try:
            backtester = self._get_quick_backtester()
            
            bt_result = backtester.quick_backtest(
                ticker=scenario.ticker,
                risk_mode=scenario.risk_mode,
                is_crypto=scenario.is_crypto,
                days=scenario.days,
                trade_mode=scenario.trade_mode,
            )
            
            # A scenario that could not run (engine error, no/too little price
            # data) is a failed scenario. It used to come back as a clean
            # "0 signals" result and was counted as PASSED.
            if bt_result.get("error"):
                result.error = str(bt_result["error"])
            elif bt_result.get("insufficient_data"):
                result.error = (
                    f"Insufficient price data ({bt_result.get('bars') or 0} bars; "
                    f"{bt_result.get('min_bars_required') or 100} needed)"
                )

            # Extract results
            result.wins = bt_result.get("wins", 0)
            result.losses = bt_result.get("losses", 0)
            result.total_signals = bt_result.get("total_signals", 0)
            result.win_rate = bt_result.get("win_rate", 0.0)
            result.signals = bt_result.get("signals", [])

            # Calculate additional metrics
            if result.signals:
                result.avg_win_pct, result.avg_loss_pct = self._calculate_avg_returns(result.signals)
                result.profit_factor = self._calculate_profit_factor(result.signals)
                result.max_drawdown = self._calculate_max_drawdown(result.signals)
                result.sharpe_ratio = self._calculate_sharpe(result.signals)
            
        except Exception as e:
            result.error = str(e)
            logger.error(f"Error running scenario {scenario.name}: {e}")
        
        result.execution_time_ms = (time.time() - start_time) * 1000
        return result
    
    def _validate_result(self, result: BacktestResult) -> List[str]:
        """Validate backtest result for anomalies."""
        errors = []
        
        if result.error:
            errors.append(f"Scenario error: {result.error}")
            return errors
        
        # === METRIC SANITY CHECKS (ERRORS) ===
        
        # Check for non-negative counts
        if result.wins < 0:
            errors.append(
                f"{result.scenario.name}: Invalid wins count ({result.wins}) - must be >= 0"
            )
        if result.losses < 0:
            errors.append(
                f"{result.scenario.name}: Invalid losses count ({result.losses}) - must be >= 0"
            )
        if result.total_signals < 0:
            errors.append(
                f"{result.scenario.name}: Invalid total_signals ({result.total_signals}) - must be >= 0"
            )
        
        # === FINITENESS CHECKS (NaN/Inf protection) ===
        if not math.isfinite(result.win_rate):
            errors.append(
                f"{result.scenario.name}: win_rate is not finite ({result.win_rate})"
            )
        elif result.win_rate < 0 or result.win_rate > 100:
            errors.append(
                f"{result.scenario.name}: Invalid win_rate ({result.win_rate:.1f}%) - must be between 0 and 100"
            )
        
        if not math.isfinite(result.max_drawdown):
            errors.append(
                f"{result.scenario.name}: max_drawdown is not finite ({result.max_drawdown})"
            )
        
        if not math.isfinite(result.sharpe_ratio):
            errors.append(
                f"{result.scenario.name}: sharpe_ratio is not finite ({result.sharpe_ratio})"
            )
        
        if result.profit_factor is not None and not math.isfinite(result.profit_factor):
            errors.append(
                f"{result.scenario.name}: profit_factor is not finite"
            )
        
        # Check for reasonable signal count
        if result.total_signals > 0:
            if result.total_signals > self.VALIDATION_THRESHOLDS["max_signals"]:
                errors.append(
                    f"{result.scenario.name}: Too many signals ({result.total_signals}) - "
                    f"possible bug in signal generation"
                )
        
        # Check for data consistency: wins + losses == total_signals
        if result.wins + result.losses != result.total_signals:
            errors.append(
                f"{result.scenario.name}: Inconsistent results - "
                f"wins({result.wins}) + losses({result.losses}) != total({result.total_signals})"
            )
        
        # Check len(signals) matches total_signals
        if len(result.signals) != result.total_signals:
            errors.append(
                f"{result.scenario.name}: Signal count mismatch - "
                f"len(signals)={len(result.signals)} != total_signals={result.total_signals}"
            )
        
        # Check win rate calculation
        if result.total_signals > 0:
            expected_win_rate = (result.wins / result.total_signals) * 100
            if math.isfinite(result.win_rate) and abs(expected_win_rate - result.win_rate) > 0.1:
                errors.append(
                    f"{result.scenario.name}: Win rate calculation error - "
                    f"expected {expected_win_rate:.1f}%, got {result.win_rate:.1f}%"
                )
        
        return errors

    def _check_warnings(self, result: BacktestResult) -> List[str]:
        """Check for warning conditions."""
        warnings = []
        
        if result.total_signals == 0:
            warnings.append(
                f"{result.scenario.name}: No signals generated - "
                f"consider adjusting parameters or check data availability"
            )
        elif result.total_signals < self.VALIDATION_THRESHOLDS["min_signals"]:
            warnings.append(
                f"{result.scenario.name}: Low signal count ({result.total_signals}) - "
                f"might not be statistically significant"
            )
        
        if result.win_rate < self.VALIDATION_THRESHOLDS["warning_win_rate_low"]:
            warnings.append(
                f"{result.scenario.name}: Low win rate ({result.win_rate:.1f}%) - "
                f"strategy might need optimization"
            )
        elif result.win_rate > self.VALIDATION_THRESHOLDS["warning_win_rate_high"]:
            warnings.append(
                f"{result.scenario.name}: Very high win rate ({result.win_rate:.1f}%) - "
                f"check for potential overfitting or data issues"
            )
        
        if result.profit_factor is None:
            warnings.append(
                f"{result.scenario.name}: Profit factor unavailable because no losing trade was observed"
            )
        
        # === EXTREME AVG WIN/LOSS CHECK ===
        # Flag extreme avg win/loss (> 50% per trade) as likely data/price-scale issues
        extreme_threshold = 50.0  # 50% per trade is suspicious for most instruments
        
        if abs(result.avg_win_pct) > extreme_threshold:
            warnings.append(
                f"{result.scenario.name}: Extreme avg win ({result.avg_win_pct:.1f}% per trade) - "
                f"likely data quality or price-scale issue"
            )
        
        if abs(result.avg_loss_pct) > extreme_threshold:
            warnings.append(
                f"{result.scenario.name}: Extreme avg loss ({result.avg_loss_pct:.1f}% per trade) - "
                f"likely data quality or price-scale issue"
            )
        
        # Execution time warning
        if result.execution_time_ms > 30000:  # 30 seconds
            warnings.append(
                f"{result.scenario.name}: Slow execution ({result.execution_time_ms/1000:.1f}s) - "
                f"consider optimizing data fetching"
            )
        
        return warnings
    
    @staticmethod
    def _trade_return_pct(sig: Dict, entry: float, exit_price: float) -> float:
        """Percent return of one closed trade, NET of commission when known.

        The entry/exit fills already carry slippage but not commission, while
        the engine decides WIN/LOSS on net pnl. Deriving every metric from the
        price move alone therefore mixed a net win/loss label with a
        before-commission magnitude (a trade that wins before commission and
        loses after it was averaged into the losses with its positive move).
        """
        try:
            qty = float(sig.get("qty") or 0.0)
            net_pnl = sig.get("net_pnl")
            if net_pnl is not None and qty > 0 and float(entry) > 0:
                return float(net_pnl) / (float(entry) * qty) * 100
        except (TypeError, ValueError):
            pass
        if sig.get("type", "BUY") == "BUY":
            return ((exit_price - entry) / entry) * 100
        return ((entry - exit_price) / entry) * 100

    def _calculate_avg_returns(self, signals: List[Dict]) -> Tuple[float, float]:
        """Calculate average win and loss percentages."""
        wins = []
        losses = []

        for sig in signals:
            entry = sig.get("entry", 0)
            exit_price = sig.get("exit_price", 0)
            result_type = sig.get("result", "")

            if not entry or not exit_price:
                continue

            pct = self._trade_return_pct(sig, entry, exit_price)

            if result_type == "WIN":
                wins.append(abs(pct))
            else:
                losses.append(abs(pct))
        
        avg_win = statistics.mean(wins) if wins else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        
        return avg_win, avg_loss
    
    def _calculate_profit_factor(self, signals: List[Dict]) -> Optional[float]:
        """
        Calculate profit factor using PERCENT RETURNS (not raw price deltas).
        This makes PF comparable across instruments with different price scales.
        
        Formula: sum(winning_percent_returns) / sum(losing_percent_returns)
        
        Returns:
            Optional[float]: finite profit factor, or ``None`` when no losing
            trade has been observed.
        """
        gross_profit_pct = 0.0  # Sum of positive percent returns
        gross_loss_pct = 0.0    # Sum of absolute negative percent returns
        
        for sig in signals:
            entry = sig.get("entry", 0)
            exit_price = sig.get("exit_price", 0)

            if not entry or entry == 0 or not exit_price:
                continue
            
            # Calculate percent return (normalized by entry price, net of costs)
            pct_return = self._trade_return_pct(sig, entry, exit_price)

            if pct_return > 0:
                gross_profit_pct += pct_return
            else:
                gross_loss_pct += abs(pct_return)
        
        # A finite ratio cannot be measured without a losing trade.
        if gross_loss_pct == 0:
            return None
        
        return gross_profit_pct / gross_loss_pct

    def _calculate_max_drawdown(self, signals: List[Dict]) -> float:
        """Calculate maximum drawdown percentage."""
        if not signals:
            return 0.0
        
        # Simulate equity curve
        equity = 100.0  # Start with 100
        peak = 100.0
        max_dd = 0.0
        
        for sig in signals:
            entry = sig.get("entry", 0)
            exit_price = sig.get("exit_price", 0)

            if not entry or not exit_price:
                continue

            pct_change = self._trade_return_pct(sig, entry, exit_price)

            equity *= (1 + pct_change / 100)
            peak = max(peak, equity)
            
            drawdown = ((peak - equity) / peak) * 100
            max_dd = max(max_dd, drawdown)
        
        return max_dd
    
    def _calculate_sharpe(self, signals: List[Dict], risk_free_rate: float = 0.0) -> float:
        """Per-trade Sharpe ratio (mean / stdev of trade returns).

        NOT annualised: trades are irregular in time, so there is no bar
        interval to scale by. Do not compare it with an annualised Sharpe.
        """
        returns = []
        
        for sig in signals:
            entry = sig.get("entry", 0)
            exit_price = sig.get("exit_price", 0)

            if not entry or not exit_price:
                continue

            ret = self._trade_return_pct(sig, entry, exit_price)

            returns.append(ret)
        
        if len(returns) < 2:
            return 0.0
        
        avg_return = statistics.mean(returns)
        std_return = statistics.stdev(returns)
        
        if std_return == 0:
            return 0.0
        
        return (avg_return - risk_free_rate) / std_return
    
    def _generate_summary(self, report: ValidationReport) -> Dict:
        """Generate summary statistics from validation report."""
        if not report.results:
            return {}
        
        valid_results = [r for r in report.results if not r.error and r.total_signals > 0]
        
        if not valid_results:
            return {
                "status": "NO_VALID_RESULTS",
                "message": "No valid backtest results to summarize",
            }
        
        # Aggregate metrics - filter out non-finite values
        all_win_rates = [r.win_rate for r in valid_results if math.isfinite(r.win_rate)]
        all_signals = [r.total_signals for r in valid_results]
        
        # Filter profit factors: exclude inf and non-positive values
        all_profit_factors = [
            r.profit_factor for r in valid_results 
            if r.profit_factor is not None and math.isfinite(r.profit_factor) and r.profit_factor > 0
        ]
        
        # Best performing scenarios (only consider finite values)
        finite_wr_results = [r for r in valid_results if math.isfinite(r.win_rate)]
        
        best_by_winrate = max(finite_wr_results, key=lambda x: x.win_rate) if finite_wr_results else None
        best_by_signals = max(valid_results, key=lambda x: x.total_signals)
        
        # Use MEDIAN for profit factor to reduce outlier impact
        median_pf = statistics.median(all_profit_factors) if all_profit_factors else None
        mean_pf = statistics.mean(all_profit_factors) if all_profit_factors else None
        
        summary = {
            "status": "COMPLETED",
            "total_signals_all": sum(all_signals),
            "avg_win_rate": statistics.mean(all_win_rates) if all_win_rates else 0,
            "median_win_rate": statistics.median(all_win_rates) if all_win_rates else 0,
            "win_rate_std": statistics.stdev(all_win_rates) if len(all_win_rates) > 1 else 0,
            "avg_signals_per_scenario": statistics.mean(all_signals) if all_signals else 0,
            "median_profit_factor": median_pf,  # Use median instead of mean
            "avg_profit_factor": mean_pf,       # Keep for reference but prefer median
            "pass_rate": (report.passed_scenarios / report.total_scenarios * 100) if report.total_scenarios else 0,
        }
        
        if best_by_winrate:
            summary["best_scenario_by_winrate"] = {
                "name": best_by_winrate.scenario.name,
                "win_rate": best_by_winrate.win_rate,
            }
        if best_by_signals:
            summary["best_scenario_by_signals"] = {
                "name": best_by_signals.scenario.name,
                "signals": best_by_signals.total_signals,
            }
        
        return summary

    def quick_test(self, ticker: str = "BTC-USD") -> Dict:
        """
        Run a quick test to verify backtest system is working.
        
        Args:
            ticker: Ticker to test (default BTC-USD for fast crypto data)
            
        Returns:
            Quick test result dictionary
        """
        import time
        
        logger.info(f"Running quick backtest test for {ticker}...")
        start = time.time()
        
        try:
            backtester = self._get_quick_backtester()
            
            result = backtester.quick_backtest(
                ticker=ticker,
                risk_mode="MEDIUM",
                is_crypto=ticker.endswith("-USD") or ticker.endswith("-USDC") or ticker.endswith("USDT"),
                days=3,
                trade_mode="PULLBACK",
            )
            
            elapsed = time.time() - start

            # The wrapper reports engine errors and missing data in the result
            # instead of raising; "SUCCESS ... completed successfully" was
            # returned for a test that never ran.
            failure = result.get("error") or (
                f"Insufficient price data ({result.get('bars') or 0} bars)"
                if result.get("insufficient_data") else None
            )
            if failure:
                return {
                    "status": "FAILED",
                    "ticker": ticker,
                    "error": str(failure),
                    "execution_time_seconds": round(elapsed, 2),
                    "message": f"Backtest failed: {failure}",
                }

            return {
                "status": "SUCCESS",
                "ticker": ticker,
                "wins": result.get("wins", 0),
                "losses": result.get("losses", 0),
                "total_signals": result.get("total_signals", 0),
                "win_rate": result.get("win_rate", 0),
                "execution_time_seconds": round(elapsed, 2),
                "message": f"Backtest completed successfully in {elapsed:.2f}s",
            }
            
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"Quick test failed: {e}")
            return {
                "status": "FAILED",
                "ticker": ticker,
                "error": str(e),
                "execution_time_seconds": round(elapsed, 2),
                "message": f"Backtest failed: {e}",
            }
    
    def compare_strategies(
        self,
        ticker: str,
        days: int = 7
    ) -> Dict:
        """
        Compare all strategy combinations for a single ticker.
        
        Args:
            ticker: Ticker to test
            days: Backtest period in days
            
        Returns:
            Comparison results for all strategy combinations
        """
        results = []
        is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC")
        
        for risk_mode in ["STRONG", "MEDIUM", "RELAX"]:
            for trade_mode in ["PULLBACK", "BREAKOUT", "CONTRA"]:
                scenario = BacktestScenario(
                    name=f"{ticker}_{risk_mode}_{trade_mode}",
                    ticker=ticker,
                    days=days,
                    risk_mode=risk_mode,
                    trade_mode=trade_mode,
                    is_crypto=is_crypto,
                )
                
                result = self._run_scenario(scenario)
                results.append({
                    "risk_mode": risk_mode,
                    "trade_mode": trade_mode,
                    "wins": result.wins,
                    "losses": result.losses,
                    "total_signals": result.total_signals,
                    "win_rate": result.win_rate,
                    "profit_factor": result.profit_factor,
                    "execution_time_ms": result.execution_time_ms,
                })
        
        # Find best combination
        valid_results = [r for r in results if r["total_signals"] > 0]
        best_by_winrate = max(valid_results, key=lambda x: x["win_rate"]) if valid_results else None
        pf_results = [r for r in valid_results if r.get("profit_factor") is not None]
        best_by_pf = max(pf_results, key=lambda x: x["profit_factor"]) if pf_results else None
        
        return {
            "ticker": ticker,
            "days": days,
            "strategies": results,
            "best_by_win_rate": best_by_winrate,
            "best_by_profit_factor": best_by_pf,
            "recommendation": self._get_recommendation(best_by_winrate, best_by_pf),
        }
    
    def _get_recommendation(self, best_wr: Optional[Dict], best_pf: Optional[Dict]) -> str:
        """Generate strategy recommendation."""
        if not best_wr and not best_pf:
            return "Insufficient data for recommendation. Try extending the backtest period."
        
        if best_wr and best_pf:
            if best_wr == best_pf:
                return f"Recommended: {best_wr['risk_mode']}/{best_wr['trade_mode']} " \
                       f"(Best win rate {best_wr['win_rate']:.1f}% and profit factor)"
            else:
                return f"Conservative: {best_wr['risk_mode']}/{best_wr['trade_mode']} " \
                       f"(Win rate {best_wr['win_rate']:.1f}%) | " \
                       f"Aggressive: {best_pf['risk_mode']}/{best_pf['trade_mode']} " \
                       f"(Profit factor {best_pf['profit_factor']:.2f})"
        
        return "Unable to make recommendation due to limited data."

    @staticmethod
    def _strategy_to_trade_mode(strategy_id: str) -> str:
        sid = str(strategy_id or "").upper().strip()
        mapping = {
            "S1_TREND_PULLBACK": "PULLBACK",
            "S2_BREAKOUT_RETEST": "BREAKOUT",
            "S3_VOL_SQUEEZE": "BREAKOUT",
            "S4_MEAN_REVERSION": "CONTRA",
            "S5_MOMENTUM_CONTINUATION": "BREAKOUT",
            "S6_FX_SESSION_BREAKOUT": "BREAKOUT",
        }
        return mapping.get(sid, "PULLBACK")

    @staticmethod
    def _direction_matches(direction: str, signal_type: str) -> bool:
        d = str(direction or "").upper().strip()
        s = str(signal_type or "").upper().strip()
        if d == "LONG":
            return s in {"BUY", "LONG"}
        if d == "SHORT":
            return s in {"SELL", "SHORT"}
        return True

    @staticmethod
    def _summarize_directional(result: Dict[str, Any], direction: str) -> Dict[str, Any]:
        signals = result.get("signals", []) if isinstance(result, dict) else []
        selected = [s for s in signals if isinstance(s, dict) and BacktestAgent._direction_matches(direction, s.get("type"))]
        if not selected:
            return {
                "wins": 0,
                "losses": 0,
                "sample_size": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "expectancy_R": 0.0,
                "avg_R": 0.0,
                "max_drawdown": 0.0,
            }

        wins = 0
        losses = 0
        r_values: List[float] = []
        gp = 0.0
        gl = 0.0
        equity = 100.0
        peak = 100.0
        max_dd = 0.0

        for trade in selected:
            r_mult = trade.get("r_multiple")
            if r_mult is None:
                # Derive R from fields the backtester ACTUALLY produces.
                # core/backtester.py signal dict emits entry/sl/qty/net_pnl/
                # result -- NOT r_multiple/pnl_pct. Pre-fix fell back to
                # pnl_pct/2.0 using a nonexistent key -> 0.0 -> every trade
                # counted as r=0.0 -> win_rate=1.0, profit_factor=0.0,
                # expectancy_R=0.0 for EVERY V2 strategy, and the research agent
                # (same cache key as feedback_loop) overwrote live-accumulated
                # performance with these bogus metrics every ~12h. Mirror
                # QuickBacktester._compute_trade_kpis (quick_backtester.py).
                entry = float(trade.get("entry", 0.0) or 0.0)
                sl = float(trade.get("sl", 0.0) or 0.0)
                net_pnl = float(trade.get("net_pnl", 0.0) or 0.0)
                qty = float(trade.get("qty", 1.0) or 1.0)
                risk = abs(entry - sl) * qty
                r_mult = (net_pnl / risk) if risk > 0 else 0.0
            try:
                r = float(r_mult)
            except Exception:
                r = 0.0
            r_values.append(r)
            # Classify via the backtester's own `result` field (it already
            # accounts for net_pnl incl. commission); fall back to r>=0 only
            # when result is absent (external trade dicts without it).
            res = str(trade.get("result") or "").upper()
            if res == "WIN":
                wins += 1
                gp += max(r, 0.0)
            elif res == "LOSS":
                losses += 1
                gl += abs(min(r, 0.0))
            elif r >= 0:
                wins += 1
                gp += r
            else:
                losses += 1
                gl += abs(r)

            equity *= (1.0 + (r * 0.01))
            peak = max(peak, equity)
            dd = ((peak - equity) / peak) * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        sample = wins + losses
        wr = (wins / sample) if sample > 0 else 0.0
        # No finite profit factor exists until at least one loss is observed.
        pf = (gp / gl) if gl > 0 else None
        exp_r = sum(r_values) / sample if sample > 0 else 0.0
        avg_r = exp_r

        return {
            "wins": wins,
            "losses": losses,
            "sample_size": sample,
            "win_rate": wr,
            "profit_factor": round(pf, 6) if pf is not None else None,
            "expectancy_R": exp_r,
            "avg_R": avg_r,
            "max_drawdown": max_dd,
        }

    def run_v2_quick(
        self,
        ticker: str,
        strategy_id: str,
        direction: str,
        asset_class: str,
        days: int = 90,
        costs: Optional[Dict[str, float]] = None,
        variant_mode: str = "STRICT",
    ) -> Dict[str, Any]:
        """
        Run Backtest-Lite for one V2 strategy + direction with realistic costs.
        """
        backtester = self._get_quick_backtester()
        is_crypto = str(asset_class or "").upper() == "CRYPTO_FUTURES"
        trade_mode = self._strategy_to_trade_mode(strategy_id)
        c = costs or {}
        raw = backtester.quick_backtest(
            ticker=ticker,
            risk_mode="MEDIUM",
            is_crypto=is_crypto,
            days=max(5, int(days or 90)),
            trade_mode=trade_mode,
            variant_mode=variant_mode,
            commission_bps=float(c.get("commission_bps", 5.0)),
            entry_slippage_bps=float(c.get("entry_slippage_bps", c.get("slippage_tp_bps", 0.0))),
            slippage_sl_bps=float(c.get("slippage_sl_bps", 2.0)),
            slippage_tp_bps=float(c.get("slippage_tp_bps", 0.0)),
        )
        summary = self._summarize_directional(raw, direction=direction)
        summary["source_trade_mode"] = trade_mode
        summary["costs_used"] = {
            "commission_bps": float(c.get("commission_bps", 5.0)),
            "entry_slippage_bps": float(c.get("entry_slippage_bps", c.get("slippage_tp_bps", 0.0))),
            "slippage_sl_bps": float(c.get("slippage_sl_bps", 2.0)),
            "slippage_tp_bps": float(c.get("slippage_tp_bps", 0.0)),
        }
        summary["raw_total_signals"] = int(raw.get("total_signals", 0) or 0)
        return summary

    def run_v2_walk_forward(
        self,
        ticker: str,
        strategy_id: str,
        direction: str,
        asset_class: str,
        days: int = 90,
        train_days: int = 20,
        test_days: int = 10,
        max_windows: int = 4,
        costs: Optional[Dict[str, float]] = None,
        variant_mode: str = "STRICT",
    ) -> Dict[str, Any]:
        """
        Walk-forward helper for V2 strategy stability/overfit penalty.
        """
        try:
            import pandas as pd

            backtester = self._get_quick_backtester()
            is_crypto = str(asset_class or "").upper() == "CRYPTO_FUTURES"
            trade_mode = self._strategy_to_trade_mode(strategy_id)
            df, warnings = backtester.fetch_backtest_data(ticker=ticker, is_crypto=is_crypto, days=max(20, int(days or 90)), ui_interval="5min")
            if df is None:
                return {"windows": [], "avg_train_expectancy_R": 0.0, "avg_test_expectancy_R": 0.0, "overfit_penalty": 100.0, "warnings": warnings}

            df = df.copy()
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp")
            train_td = pd.Timedelta(days=max(5, int(train_days or 20)))
            test_td = pd.Timedelta(days=max(3, int(test_days or 10)))
            step = test_td

            c = costs or {}
            cursor = df["timestamp"].min()
            end_ts = df["timestamp"].max()
            windows: List[Dict[str, Any]] = []
            while cursor + train_td + test_td <= end_ts and len(windows) < max(1, int(max_windows or 4)):
                train_start = cursor
                train_end = cursor + train_td
                test_end = train_end + test_td
                train_df = df[(df["timestamp"] >= train_start) & (df["timestamp"] < train_end)]
                test_df = df[(df["timestamp"] >= train_end) & (df["timestamp"] < test_end)]
                cursor += step
                if len(train_df) < 80 or len(test_df) < 40:
                    continue

                train_raw = backtester.quick_backtest(
                    ticker=ticker,
                    risk_mode="MEDIUM",
                    is_crypto=is_crypto,
                    days=train_days,
                    trade_mode=trade_mode,
                    df=train_df,
                    variant_mode=variant_mode,
                    commission_bps=float(c.get("commission_bps", 5.0)),
                    entry_slippage_bps=float(c.get("entry_slippage_bps", c.get("slippage_tp_bps", 0.0))),
                    slippage_sl_bps=float(c.get("slippage_sl_bps", 2.0)),
                    slippage_tp_bps=float(c.get("slippage_tp_bps", 0.0)),
                )
                test_raw = backtester.quick_backtest(
                    ticker=ticker,
                    risk_mode="MEDIUM",
                    is_crypto=is_crypto,
                    days=test_days,
                    trade_mode=trade_mode,
                    df=test_df,
                    variant_mode=variant_mode,
                    commission_bps=float(c.get("commission_bps", 5.0)),
                    entry_slippage_bps=float(c.get("entry_slippage_bps", c.get("slippage_tp_bps", 0.0))),
                    slippage_sl_bps=float(c.get("slippage_sl_bps", 2.0)),
                    slippage_tp_bps=float(c.get("slippage_tp_bps", 0.0)),
                )
                train_sum = self._summarize_directional(train_raw, direction=direction)
                test_sum = self._summarize_directional(test_raw, direction=direction)
                windows.append(
                    {
                        "train_start": train_start.isoformat(),
                        "train_end": train_end.isoformat(),
                        "test_end": test_end.isoformat(),
                        "train": train_sum,
                        "test": test_sum,
                    }
                )

            if not windows:
                return {"windows": [], "avg_train_expectancy_R": 0.0, "avg_test_expectancy_R": 0.0, "overfit_penalty": 80.0, "warnings": warnings}

            avg_train = sum(float(w["train"].get("expectancy_R", 0.0) or 0.0) for w in windows) / len(windows)
            avg_test = sum(float(w["test"].get("expectancy_R", 0.0) or 0.0) for w in windows) / len(windows)
            overfit_penalty = max(0.0, (avg_train - avg_test) * 100.0)
            return {
                "windows": windows,
                "avg_train_expectancy_R": avg_train,
                "avg_test_expectancy_R": avg_test,
                "overfit_penalty": overfit_penalty,
            }
        except Exception as e:
            logger.error(f"run_v2_walk_forward error ({ticker}/{strategy_id}/{direction}): {e}")
            return {"windows": [], "avg_train_expectancy_R": 0.0, "avg_test_expectancy_R": 0.0, "overfit_penalty": 100.0, "error": str(e)}
    
    def save_report(self, report: ValidationReport, filepath: Optional[str] = None) -> str:
        """Save validation report to file."""
        if not filepath:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = f"backtest_validation_{timestamp}.json"
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, default=str)
        
        logger.info(f"Report saved to {filepath}")
        return filepath


# Singleton instance
_backtest_agent_instance: Optional[BacktestAgent] = None


def get_backtest_agent(config_manager=None) -> BacktestAgent:
    """Get or create the backtest agent singleton."""
    global _backtest_agent_instance
    if _backtest_agent_instance is None:
        _backtest_agent_instance = BacktestAgent(config_manager)
    return _backtest_agent_instance
