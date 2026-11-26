"""Adapters that compose the three existing backtest engines.

The faithful and exploratory engines already execute on the next bar.  The
legacy V1 backtester enters at the signal bar's close; its adapter therefore
fails closed when it produces trades instead of mislabelling unsafe output as
look-ahead-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from .engine import BacktestEngine, NoLookAheadError
from .metrics import compute_metrics, merge_with_legacy
from .types import BacktestConfig, BacktestResult, EvidenceLevel, Trade


class AdapterDataError(ValueError):
    """Raised when a legacy result lacks evidence required for a V9 metric."""


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum) or hasattr(value, "value"):
        return str(value.value)
    return str(value or "")


def _ratio(value: Any) -> float:
    number = float(value or 0.0)
    return number / 100.0 if abs(number) > 1.0 else number


def _cost_bps(cost_model: Mapping[str, Any], reason: str) -> tuple[float, float]:
    entry = float(cost_model.get("entry_slippage_bps", 0.0) or 0.0)
    exit_key = (
        "stop_or_trailing_exit_slippage_bps"
        if str(reason).lower() in {"stop_loss", "trailing_stop"}
        else "take_profit_or_time_exit_slippage_bps"
    )
    exit_value = float(cost_model.get(exit_key, 0.0) or 0.0)
    return entry, exit_value


def _estimated_slippage(
    *,
    entry_price: float,
    exit_price: float,
    size: float,
    cost_model: Mapping[str, Any],
    reason: str,
) -> float:
    entry_bps, exit_bps = _cost_bps(cost_model, reason)
    return (
        abs(entry_price * size) * entry_bps / 10_000.0
        + abs(exit_price * size) * exit_bps / 10_000.0
    )


def _raw_mapping(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    attributes = dict(vars(raw))
    if attributes:
        return attributes
    return {
        name: getattr(raw, name)
        for name in dir(raw)
        if not name.startswith("_") and not callable(getattr(raw, name))
    }


def _item_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    attributes = dict(vars(item))
    if attributes:
        return attributes
    return {
        name: getattr(item, name)
        for name in dir(item)
        if not name.startswith("_") and not callable(getattr(item, name))
    }


def _timestamp_index(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, row in enumerate(rows):
        timestamp = row.get("timestamp")
        if timestamp is not None:
            result[str(timestamp)] = index
    return result


class _BaseAdapter:
    validation_scope = "abstract"
    is_authoritative = False
    adapter_name = "abstract"

    def run(self, config: BacktestConfig) -> BacktestResult:
        raw_value = self._run_raw(config)
        audit = _raw_mapping(raw_value)
        trades = tuple(self._lift_trades(raw_value, audit, config))
        metrics = self._lift_metrics(raw_value, audit, trades, config)
        audit["adapter"] = self.adapter_name
        audit["validation_scope"] = self.validation_scope
        audit["is_authoritative"] = self.is_authoritative
        return BacktestResult(
            config=config,
            metrics=metrics,
            trades=trades,
            evidence_level=EvidenceLevel.TICKER,
            no_look_ahead=True,
            raw=audit,
        )

    def _run_raw(self, config: BacktestConfig) -> Any:
        raise NotImplementedError

    def _lift_trades(
        self,
        raw_value: Any,
        audit: Mapping[str, Any],
        config: BacktestConfig,
    ) -> Sequence[Trade]:
        return ()

    def _lift_metrics(
        self,
        raw_value: Any,
        audit: Mapping[str, Any],
        trades: Sequence[Trade],
        config: BacktestConfig,
    ):
        return compute_metrics(
            trades,
            initial_capital=config.initial_capital,
            validation_scope=self.validation_scope,
            is_authoritative=self.is_authoritative,
        )


class FaithfulReplayAdapter(_BaseAdapter):
    validation_scope = "faithful_v2_live_replay"
    is_authoritative = True
    adapter_name = "faithful_replay"

    def __init__(
        self,
        *,
        runner: Optional[Callable[..., Any]] = None,
        dataframe: Any = None,
        config_manager: Any = None,
        days: int = 30,
    ) -> None:
        self._runner = runner
        self._dataframe = dataframe
        self._config_manager = config_manager
        self._days = days

    def _run_raw(self, config: BacktestConfig) -> Any:
        dataframe = self._dataframe
        if dataframe is not None and config.out_of_sample_range is not None:
            oos_start, oos_end = config.out_of_sample_range
            context_start = config.train_range[0] if config.train_range is not None else max(0, oos_start - 60)
            dataframe = dataframe.iloc[context_start:oos_end].copy()
        kwargs = {
            "ticker": config.ticker,
            "days": self._days,
            "tp_pct_override": config.tp_pct,
            "sl_pct_override": config.sl_pct,
            "df": dataframe,
            "config_manager": self._config_manager,
        }
        if self._runner is not None:
            return self._runner(**kwargs)
        from core.strategy_backtester import backtest

        ticker = kwargs.pop("ticker")
        return backtest(ticker, **kwargs)

    def _dataframe_timestamp_map(self) -> dict[str, int]:
        if self._dataframe is None:
            return {}
        frame = self._dataframe.reset_index(drop=False)
        timestamp_column = "dt" if "dt" in frame.columns else frame.columns[0]
        return {
            value.isoformat() if hasattr(value, "isoformat") else str(value): index
            for index, value in enumerate(frame[timestamp_column])
        }

    def _lift_trades(self, raw_value, audit, config) -> Sequence[Trade]:
        rows = audit.get("trade_list") or []
        timestamp_map = self._dataframe_timestamp_map()
        lifted: list[Trade] = []
        for row in rows:
            direction = str(row.get("dir") or row.get("direction") or "").upper()
            if config.direction != "BOTH" and direction != config.direction:
                continue
            entry_bar = row.get("entry_bar")
            exit_bar = row.get("exit_bar")
            if entry_bar is None:
                entry_bar = timestamp_map.get(str(row.get("entry_time")))
            if exit_bar is None:
                exit_bar = timestamp_map.get(str(row.get("exit_time")))
            if entry_bar is None or exit_bar is None:
                raise AdapterDataError(
                    "faithful replay returned trades without bar-index evidence; "
                    "construct the adapter with the replay dataframe"
                )
            if config.out_of_sample_range is not None:
                oos_start, oos_end = config.out_of_sample_range
                if not (oos_start <= int(entry_bar) and int(exit_bar) < oos_end):
                    continue
            entry_price = float(row.get("entry", 0.0) or 0.0)
            exit_price = float(row.get("exit", row.get("exit_price", 0.0)) or 0.0)
            if entry_price <= 0:
                raise AdapterDataError("faithful replay trade has no positive entry price")
            notional = config.initial_capital * config.position_size_pct
            size = float(row.get("size", 0.0) or 0.0) or notional / entry_price
            reason = str(row.get("reason", row.get("exit_reason", "")) or "")
            cost_model = audit.get("cost_model") or {}
            slippage = _estimated_slippage(
                entry_price=entry_price,
                exit_price=exit_price,
                size=size,
                cost_model=cost_model,
                reason=reason,
            )
            direction_sign = 1.0 if direction == "LONG" else -1.0
            filled_gross = (exit_price - entry_price) * size * direction_sign
            gross_before_modelled_slippage = filled_gross + slippage
            commission_pct = float(row.get("commission_cost_pct", 0.0) or 0.0) / 100.0
            commission = notional * commission_pct
            net_pct = float(row.get("net_pct", 0.0) or 0.0)
            lifted.append(
                Trade(
                    entry_bar=int(entry_bar),
                    exit_bar=int(exit_bar),
                    entry_time=row.get("entry_time"),
                    exit_time=row.get("exit_time"),
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    sl=row.get("stop_loss_price", row.get("sl")),
                    tp=row.get("take_profit_price", row.get("tp")),
                    size=size,
                    gross_pnl=gross_before_modelled_slippage,
                    commission=commission,
                    slippage_cost=slippage,
                    result="WIN" if net_pct > 0 else "LOSS",
                    r_multiple=float(row.get("r_net", row.get("r", 0.0)) or 0.0),
                    exit_reason=reason,
                    holding_bars=max(0, int(exit_bar) - int(entry_bar)),
                )
            )
        return lifted


class ExploratoryEngineAdapter(_BaseAdapter):
    validation_scope = "exploratory_legacy_strategy"
    is_authoritative = False
    adapter_name = "exploratory_engine"

    def __init__(
        self,
        *,
        runner: Optional[Callable[..., Any]] = None,
        lookback_days: int = 30,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._runner = runner
        self._lookback_days = lookback_days
        self._now_provider = now_provider

    def _run_raw(self, config: BacktestConfig) -> Any:
        end = self._now_provider()
        kwargs = {
            "ticker": config.ticker,
            "start_date": end - timedelta(days=self._lookback_days),
            "end_date": end,
            "initial_capital": config.initial_capital,
            "position_size_pct": config.position_size_pct,
            "commission_pct": config.commission_pct,
            "tp_pct": config.tp_pct,
            "sl_pct": config.sl_pct,
            "max_holding_bars": config.max_holding_bars,
        }
        if self._runner is not None:
            return self._runner(**kwargs)
        from core.backtest_engine import get_backtest_engine

        return get_backtest_engine().run_backtest(**kwargs)

    def _lift_trades(self, raw_value, audit, config) -> Sequence[Trade]:
        source_trades = getattr(raw_value, "trades", None)
        if source_trades is None:
            source_trades = audit.get("trades") or []
        index_by_time = _timestamp_index(audit.get("equity_curve") or [])
        cost_model = audit.get("cost_model") or {}
        lifted: list[Trade] = []
        for item in source_trades:
            row = _item_mapping(item)
            direction = _enum_value(row.get("direction", "LONG")).upper()
            if config.direction != "BOTH" and direction != config.direction:
                continue
            entry_time_value = row.get("entry_time")
            exit_time_value = row.get("exit_time")
            entry_time = entry_time_value.isoformat() if hasattr(entry_time_value, "isoformat") else str(entry_time_value)
            exit_time = exit_time_value.isoformat() if hasattr(exit_time_value, "isoformat") else str(exit_time_value)
            entry_bar = index_by_time.get(entry_time)
            exit_bar = index_by_time.get(exit_time)
            if entry_bar is None or exit_bar is None:
                raise AdapterDataError("exploratory result lacks timestamped equity evidence")
            entry_price = float(row.get("entry_price", 0.0) or 0.0)
            exit_price = float(row.get("exit_price", 0.0) or 0.0)
            size = float(row.get("quantity", row.get("size", 0.0)) or 0.0)
            reason = _enum_value(row.get("exit_reason"))
            slippage = _estimated_slippage(
                entry_price=entry_price,
                exit_price=exit_price,
                size=size,
                cost_model=cost_model,
                reason=reason,
            )
            net_pnl = float(row.get("pnl", 0.0) or 0.0)
            risk_amount = abs(entry_price - float(row.get("sl_price", 0.0) or 0.0)) * size
            lifted.append(
                Trade(
                    entry_bar=entry_bar,
                    exit_bar=exit_bar,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    sl=row.get("sl_price"),
                    tp=row.get("tp_price"),
                    size=size,
                    gross_pnl=float(row.get("gross_pnl", 0.0) or 0.0) + slippage,
                    commission=float(row.get("commission", 0.0) or 0.0),
                    slippage_cost=slippage,
                    result="WIN" if net_pnl > 0 else "LOSS",
                    r_multiple=net_pnl / risk_amount if risk_amount > 0 else 0.0,
                    exit_reason=reason,
                    holding_bars=int(row.get("holding_bars", exit_bar - entry_bar) or 0),
                )
            )
        return lifted

    def _lift_metrics(self, raw_value, audit, trades, config):
        equity_values = tuple(
            float(row["equity"])
            for row in (audit.get("equity_curve") or [])
            if isinstance(row, Mapping) and row.get("equity") is not None
        )
        normalized = dict(audit)
        normalized["win_rate"] = _ratio(audit.get("win_rate", 0.0))
        if equity_values:
            normalized["equity_curve"] = equity_values
        return merge_with_legacy(
            normalized,
            trades,
            initial_capital=config.initial_capital,
            validation_scope=self.validation_scope,
            is_authoritative=False,
        )


class LegacyBacktesterAdapter(_BaseAdapter):
    validation_scope = "legacy_v1_rsi_macd"
    is_authoritative = False
    adapter_name = "legacy_v1_backtester"

    def __init__(
        self,
        *,
        runner: Optional[Callable[..., Any]] = None,
        dataframe: Any = None,
        config_manager: Any = None,
        days: int = 30,
    ) -> None:
        self._runner = runner
        self._dataframe = dataframe
        self._config_manager = config_manager
        self._days = days

    def _run_raw(self, config: BacktestConfig) -> Any:
        kwargs = {"ticker": config.ticker, "days": self._days, "df": self._dataframe}
        if self._runner is not None:
            return self._runner(**kwargs)
        from core.backtester import Backtester
        from core.config import ConfigManager, is_crypto_ticker

        manager = self._config_manager or ConfigManager()
        return Backtester(manager).run_backtest(
            config.ticker,
            days=self._days,
            is_crypto=is_crypto_ticker(config.ticker),
            df=self._dataframe,
        )

    def run(self, config: BacktestConfig) -> BacktestResult:
        raw = self._run_raw(config)
        audit = _raw_mapping(raw)
        if audit.get("signals"):
            raise NoLookAheadError(
                "legacy V1 backtester enters at the signal bar close and cannot satisfy next-bar-open"
            )
        metrics = compute_metrics(
            (),
            initial_capital=config.initial_capital,
            validation_scope=self.validation_scope,
            is_authoritative=False,
        )
        audit.update(
            {
                "adapter": self.adapter_name,
                "legacy_engine_limit": "same-bar execution; no trades accepted by the V9 domain interface",
            }
        )
        return BacktestResult(config=config, metrics=metrics, trades=(), raw=audit)


def get_default_engine(**kwargs: Any) -> BacktestEngine:
    """Return the faithful live-replay adapter used by V9 validation."""

    return FaithfulReplayAdapter(**kwargs)
