"""core.signal_engine — §52/§53 Signal Engine surface + market-wide regime."""
from .horizons import (SignalHorizon, HorizonDefinition, HORIZON_DEFINITIONS,
                        map_mode_to_horizon, align_horizon)
from .catalog import (IndicatorStatus, IndicatorSpec, SIGNAL_CATALOG,
                      get_catalog_index, preserved_indicators)
from .regime import (MarketRegimeEngine, MarketRegimeResult, detect_macro_regime,
                     MARKET_RISK_ON, MARKET_RISK_OFF, MARKET_MIXED, MARKET_TRANSITIONING)
from .types import Signal

__all__ = [
    "SignalHorizon", "HorizonDefinition", "HORIZON_DEFINITIONS",
    "map_mode_to_horizon", "align_horizon",
    "IndicatorStatus", "IndicatorSpec", "SIGNAL_CATALOG",
    "get_catalog_index", "preserved_indicators",
    "MarketRegimeEngine", "MarketRegimeResult", "detect_macro_regime",
    "MARKET_RISK_ON", "MARKET_RISK_OFF", "MARKET_MIXED", "MARKET_TRANSITIONING",
    "Signal",
]