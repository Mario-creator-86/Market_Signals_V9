"""Single, explicit cost model used by paper execution and historical replay.

The values come from local configuration, not from an exchange account.  They
must therefore be shown as *modelled estimates* and never as realised exchange
fees.  Actual order fills, account fee tier and perpetual funding are separate
data that must be reconciled before a strategy can be promoted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from core.config import is_crypto_ticker


_KNOWN_FOREX = {
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X",
}
_KNOWN_COMMODITIES = {"GC=F", "SI=F", "CL=F", "NG=F", "HG=F", "PL=F", "PA=F", "BZ=F"}


def infer_cost_asset_class(ticker: str) -> str:
    """Map a display ticker to the configured cost bucket without V2 imports."""
    symbol = str(ticker or "").upper().strip()
    if symbol in _KNOWN_FOREX:
        return "FOREX_MAJORS"
    if symbol in _KNOWN_COMMODITIES:
        return "COMMODITIES"
    if is_crypto_ticker(symbol):
        return "CRYPTO_FUTURES"
    return "US_STOCKS"


def _finite_nonnegative(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if number < 0 or number != number or number == float("inf"):
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


@dataclass(frozen=True)
class TradingCostModel:
    """Configured per-side commission and adverse price assumptions in bps."""

    asset_class: str
    commission_bps: float
    entry_slippage_bps: float
    slippage_sl_bps: float
    slippage_tp_bps: float
    provenance: str = "configured_estimate"

    @property
    def commission_fraction(self) -> float:
        return self.commission_bps / 10_000.0

    @property
    def entry_slippage_fraction(self) -> float:
        return self.entry_slippage_bps / 10_000.0

    def exit_slippage_fraction(self, reason: str) -> float:
        normalized = str(reason or "").lower().strip()
        bps = self.slippage_sl_bps if normalized in {"stop_loss", "trailing_stop"} else self.slippage_tp_bps
        return bps / 10_000.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "commission_bps_per_side": self.commission_bps,
            "entry_slippage_bps": self.entry_slippage_bps,
            "stop_or_trailing_exit_slippage_bps": self.slippage_sl_bps,
            "take_profit_or_time_exit_slippage_bps": self.slippage_tp_bps,
            "provenance": self.provenance,
            "is_realized_exchange_fee": False,
            "reconciliation_required": "Account fee tier, actual fills and perpetual funding are not included.",
        }


def resolve_trading_cost_model(
    ticker: str,
    *,
    strategy_v2_config: Optional[Mapping[str, Any]] = None,
    asset_class: Optional[str] = None,
    overrides: Optional[Mapping[str, Any]] = None,
) -> TradingCostModel:
    """Resolve and validate a configured cost model.

    ``overrides`` is used only by explicit what-if simulations.  Callers must
    save the returned ``to_dict`` alongside results so the assumptions remain
    reproducible.  Missing configuration is an error: silently substituting a
    fee rate would make results appear more precise than their inputs permit.
    """
    resolved_asset_class = str(asset_class or infer_cost_asset_class(ticker)).upper().strip()
    config = strategy_v2_config or {}
    costs_by_asset = config.get("costs_by_asset", {}) if isinstance(config, Mapping) else {}
    configured = costs_by_asset.get(resolved_asset_class, {}) if isinstance(costs_by_asset, Mapping) else {}
    values: Dict[str, Any] = dict(configured) if isinstance(configured, Mapping) else {}
    if overrides:
        values.update(dict(overrides))

    required = (
        "commission_bps",
        "entry_slippage_bps",
        "slippage_sl_bps",
        "slippage_tp_bps",
    )
    missing = [key for key in required if key not in values]
    if missing:
        raise ValueError(
            f"Cost model for {resolved_asset_class} is incomplete; missing: {', '.join(missing)}"
        )

    return TradingCostModel(
        asset_class=resolved_asset_class,
        commission_bps=_finite_nonnegative(values["commission_bps"], "commission_bps"),
        entry_slippage_bps=_finite_nonnegative(values["entry_slippage_bps"], "entry_slippage_bps"),
        slippage_sl_bps=_finite_nonnegative(values["slippage_sl_bps"], "slippage_sl_bps"),
        slippage_tp_bps=_finite_nonnegative(values["slippage_tp_bps"], "slippage_tp_bps"),
        provenance="explicit_override_estimate" if overrides else "configured_estimate",
    )
