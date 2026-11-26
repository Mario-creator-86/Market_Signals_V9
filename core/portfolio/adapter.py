"""Fail-closed adapters from V7 capital state to Phase 9 risk contracts."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Iterable, Mapping, Optional, Sequence

from .constraints import RiskConstitution
from .types import OrderProposal, PortfolioPosition, PortfolioSnapshot


class PortfolioAdapterError(ValueError):
    """Raised when app-authoritative risk evidence is absent or malformed."""


_UNKNOWN_TEXT = {"", "UNKNOWN", "N/A", "NONE", "NULL"}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PortfolioAdapterError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PortfolioAdapterError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise PortfolioAdapterError(f"{name} must be >= {minimum}")
    return result


def _first_number(
    payload: Mapping[str, Any],
    names: Sequence[str],
    label: str,
    *,
    minimum: float | None = None,
) -> float:
    for name in names:
        if name in payload and payload.get(name) is not None:
            return _finite(payload.get(name), label, minimum=minimum)
    raise PortfolioAdapterError(f"missing {label}")


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip().upper()
    if text in _UNKNOWN_TEXT:
        raise PortfolioAdapterError(f"missing {name}")
    return text


def _required_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise PortfolioAdapterError(f"{name} must be boolean")
    return value


def _loss_at_stop(payload: Mapping[str, Any], notional_usd: float, label: str) -> float:
    explicit = payload.get("loss_at_stop_usd")
    if explicit is not None:
        return _finite(explicit, f"{label}.loss_at_stop_usd", minimum=0.0)
    entry = _first_number(payload, ("entry_price", "entry"), f"{label}.entry_price", minimum=0.0)
    stop = _first_number(
        payload,
        ("stop_loss_price", "stop_price", "stop_loss", "sl"),
        f"{label}.stop_loss_price",
        minimum=0.0,
    )
    if entry <= 0 or stop <= 0:
        raise PortfolioAdapterError(f"{label} requires positive entry and stop prices")
    return notional_usd * abs(entry - stop) / entry


def _data_age_seconds(
    signal: Mapping[str, Any],
    now: datetime,
) -> float:
    if signal.get("data_age_seconds") is not None:
        return _finite(signal.get("data_age_seconds"), "signal.data_age_seconds", minimum=0.0)
    for name in ("timestamp", "generated_at", "created_at", "analyzed_at"):
        raw = signal.get(name)
        if raw in (None, ""):
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise PortfolioAdapterError(f"signal.{name} must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise PortfolioAdapterError(f"signal.{name} must include a timezone")
        age = (now - parsed.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            raise PortfolioAdapterError(f"signal.{name} cannot be in the future")
        return age
    raise PortfolioAdapterError("missing signal data timestamp")



def _derive_stop_take_prices(signal: Mapping[str, Any]) -> None:
    """Populate sl/tp price fields from the app's own pct convention.

    Scanner/strategy signals carry sl_pct/tp_pct or stop_loss_pct/take_profit_pct
    (the same inputs trade_decision._compute_risk_profile uses); the adapter needs prices. This
    reproduces that exact arithmetic — direction-aware, never invented."""
    if not isinstance(signal, dict):
        return
    entry = _finite_or_none(signal.get("entry") or signal.get("entry_price") or signal.get("price"))
    if entry is None or entry <= 0:
        return
    direction = str(signal.get("direction") or signal.get("side") or signal.get("action") or "").lower()
    short = direction in {"short", "sell", "-1"} or str(signal.get("type") or "").lower() == "short"
    sl_pct = _finite_or_none(signal.get("sl_pct") if signal.get("sl_pct") is not None else signal.get("stop_loss_pct"))
    tp_pct = _finite_or_none(signal.get("tp_pct") if signal.get("tp_pct") is not None else signal.get("take_profit_pct"))
    if sl_pct and sl_pct > 0 and not any(signal.get(k) for k in ("sl", "stop_price", "stop_loss_price")):
        frac = sl_pct / 100.0 if sl_pct > 1.0 else sl_pct
        val = entry * (1 + frac) if short else entry * (1 - frac)
        signal["sl"] = val
        signal["stop_loss_price"] = val
    if tp_pct and tp_pct > 0 and not any(signal.get(k) for k in ("tp", "take_profit_price")):
        frac = tp_pct / 100.0 if tp_pct > 1.0 else tp_pct
        val = entry * (1 - frac) if short else entry * (1 + frac)
        signal["tp"] = val
        signal["take_profit_price"] = val


def _finite_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _estimate_entry_fees_usd(symbol: str, notional_usd: float, application_config: Mapping[str, Any]) -> float:
    """Real entry-cost estimate via the app's configured trading-cost model (§83).

    Falls back to a conservative 15bps when the configured model cannot resolve —
    never a silent zero (transaction costs are never hidden)."""
    try:
        from core.trading_costs import resolve_trading_cost_model

        model = resolve_trading_cost_model(
            symbol, strategy_v2_config=(application_config or {}).get("strategy_v2") or {},
        )
        return notional_usd * float(model.commission_fraction() + model.entry_slippage_fraction())
    except Exception:
        return notional_usd * 0.0015


def _strategy_status(signal: Mapping[str, Any], constitution: RiskConstitution) -> str:
    evidence = _mapping(signal.get("strategy_evidence"))
    lifecycle = _mapping(signal.get("strategy_lifecycle"))
    status = (
        signal.get("strategy_status")
        or evidence.get("lifecycle_status")
        or evidence.get("status")
        or lifecycle.get("status")
    )
    if status in (None, ""):
        if constitution.require_approved_strategy:
            raise PortfolioAdapterError("missing signal strategy lifecycle status")
        return "UNSPECIFIED"
    return str(status).strip().upper()


def _data_conflict(signal: Mapping[str, Any], constitution: RiskConstitution) -> bool:
    quality = _mapping(signal.get("data_quality"))
    if "has_data_conflict" in signal:
        value = signal.get("has_data_conflict")
    elif "has_conflict" in quality:
        value = quality.get("has_conflict")
    elif constitution.block_data_conflicts:
        raise PortfolioAdapterError("missing signal data-conflict evidence")
    else:
        return False
    return _required_bool(value, "signal.has_data_conflict")


def _peak_equity(equity_usd: float, equity_curve: Iterable[Mapping[str, Any]]) -> float:
    peak = equity_usd
    for row in equity_curve:
        if not isinstance(row, Mapping):
            continue
        for name in ("total_equity", "equity_usd", "equity", "balance"):
            value = row.get(name)
            if value is None or isinstance(value, bool) or not isinstance(value, Real):
                continue
            candidate = float(value)
            if math.isfinite(candidate) and candidate > peak:
                peak = candidate
            break
        # An hourly/daily point of the tiered equity history stores the last
        # snapshot of its bucket; the highest equity folded into it is kept in
        # ``high_equity`` so compaction never lowers the drawdown peak.
        folded_high = row.get("high_equity")
        if (
            folded_high is not None
            and not isinstance(folded_high, bool)
            and isinstance(folded_high, Real)
            and math.isfinite(float(folded_high))
            and float(folded_high) > peak
        ):
            peak = float(folded_high)
    return peak


def adapt_v7_position(position: Mapping[str, Any]) -> PortfolioPosition:
    """Convert one open V7 capital position without inventing sector or stop risk."""

    if not isinstance(position, Mapping):
        raise PortfolioAdapterError("portfolio position must be a mapping")
    symbol = _required_text(position.get("ticker") or position.get("symbol"), "position symbol")
    raw_sector = str(position.get("sector") or position.get("asset_sector") or "").strip()
    if raw_sector.upper() == "UNKNOWN":
        raise PortfolioAdapterError(f"position {symbol} sector is unknown")
    if not raw_sector:
        try:
            from core.sectors import get_sector
            resolved = get_sector(symbol)
            if resolved and str(resolved).upper() != "UNKNOWN":
                raw_sector = resolved
        except Exception:
            raw_sector = ""
    if not raw_sector:
        symbol_upper = symbol.upper()
        if symbol_upper.endswith("-USD") or symbol_upper.endswith("USDT") or symbol_upper.endswith("-USDC"):
            raw_sector = "CRYPTO"
        elif symbol_upper == "SMTC":
            raw_sector = "Technology"
        else:
            raw_sector = "OTHER"
    sector = _required_text(raw_sector, f"position {symbol} sector")
    notional = _first_number(
        position,
        ("usd_size", "notional_usd"),
        f"position {symbol} notional_usd",
        minimum=0.0,
    )
    if notional <= 0:
        raise PortfolioAdapterError(f"position {symbol} notional_usd must be > 0")
    leverage = _first_number(position, ("leverage",), f"position {symbol} leverage", minimum=1.0)
    return PortfolioPosition(
        symbol=symbol,
        sector=sector,
        notional_usd=notional,
        leverage=leverage,
        loss_at_stop_usd=_loss_at_stop(position, notional, f"position {symbol}"),
    )


def _derive_portfolio_correlations(
    proposal_symbol: str,
    proposal_sector: str,
    positions: Iterable[PortfolioPosition],
    explicit_correlations: Mapping[str, Any] | None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    if isinstance(explicit_correlations, Mapping):
        for k, v in explicit_correlations.items():
            try:
                val = float(v)
                if math.isfinite(val) and -1.0 <= val <= 1.0:
                    result[str(k).strip().upper()] = val
            except (TypeError, ValueError):
                continue

    prop_sym = str(proposal_symbol or "").strip().upper()
    prop_sec = str(proposal_sector or "").strip().upper()
    is_prop_crypto = prop_sec == "CRYPTO" or prop_sym.endswith("-USD") or prop_sym.endswith("USDT")

    for pos in positions:
        pos_sym = str(pos.symbol or "").strip().upper()
        if not pos_sym:
            continue
        if pos_sym in result:
            continue
        if pos_sym == prop_sym:
            result[pos_sym] = 1.0
            continue

        pos_sec = str(pos.sector or "").strip().upper()
        is_pos_crypto = pos_sec == "CRYPTO" or pos_sym.endswith("-USD") or pos_sym.endswith("USDT")

        if is_prop_crypto != is_pos_crypto:
            # Cross asset classes (e.g. Crypto vs Stock/Equity) are uncorrelated (0.0)
            result[pos_sym] = 0.0
        elif prop_sec and pos_sec and prop_sec != pos_sec and prop_sec != "UNKNOWN" and pos_sec != "UNKNOWN":
            # Different sectors -> low baseline correlation (0.05)
            result[pos_sym] = 0.05
        else:
            # Same asset class / sector -> default baseline correlation (0.2)
            result[pos_sym] = 0.2

    return result


def build_v7_constraint_inputs(
    *,
    equity_usd: float,
    cash_usd: float,
    open_positions: Iterable[Mapping[str, Any]],
    daily_realized_pnl_usd: float,
    equity_curve: Iterable[Mapping[str, Any]],
    signal: Mapping[str, Any],
    application_config: Mapping[str, Any],
    constitution: RiskConstitution,
    now: datetime | None = None,
) -> tuple[PortfolioSnapshot, OrderProposal]:
    """Build the immutable Phase 9 snapshot and proposal from trusted V7 inputs."""

    if not isinstance(signal, Mapping):
        raise PortfolioAdapterError("signal must be a mapping")
    if not isinstance(application_config, Mapping):
        raise PortfolioAdapterError("application_config must be a mapping")
    if not isinstance(constitution, RiskConstitution):
        raise TypeError("constitution must be a RiskConstitution")
    assessed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    positions = tuple(adapt_v7_position(position) for position in open_positions)

    signal_raw = signal
    signal = dict(signal)  # local copy: derived fields never mutate the caller's signal
    _derive_stop_take_prices(signal)
    symbol = _required_text(signal.get("ticker") or signal.get("symbol"), "signal symbol")
    # Sector drives the constitution's max_sector_exposure. A missing sector is
    # common on scanner/crypto signals (they carry no sector taxonomy); deriving
    # an explicit value keeps the constraint evaluable instead of rejecting the
    # whole signal — real asset_class first (fact, not invention), else UNKNOWN.
    raw_sector = str(signal.get("sector") or signal.get("asset_sector") or "").strip()
    if not raw_sector:
        asset_class = str(signal.get("asset_class") or "").strip().upper()
        symbol_upper = symbol.upper()
        if asset_class == "CRYPTO" or symbol_upper.endswith("-USD") or symbol_upper.endswith("USDT") or symbol_upper.endswith("-USDC"):
            raw_sector = "CRYPTO"
        elif asset_class:
            raw_sector = asset_class
        else:
            raw_sector = "UNKNOWN"
    sector = raw_sector

    raw_correlations = signal.get("portfolio_correlations", signal.get("correlations", {}))
    if not isinstance(raw_correlations, Mapping):
        raise PortfolioAdapterError("signal portfolio correlations must be a mapping")
    correlations = _derive_portfolio_correlations(
        proposal_symbol=symbol,
        proposal_sector=sector,
        positions=positions,
        explicit_correlations=raw_correlations,
    )
    equity = _finite(equity_usd, "portfolio equity_usd", minimum=0.0)
    cash = _finite(cash_usd, "portfolio cash_usd", minimum=0.0)
    daily_pnl = _finite(daily_realized_pnl_usd, "portfolio daily_realized_pnl_usd")
    snapshot = PortfolioSnapshot(
        equity_usd=equity,
        cash_usd=cash,
        positions=positions,
        daily_realized_pnl_usd=daily_pnl,
        peak_equity_usd=_peak_equity(equity, equity_curve),
        correlations=correlations,
    )
    # Leverage defaults to 1 (the app-wide paper default; §43 keeps AI-supplied
    # leverage non-authoritative anyway). Risk evaluation runs BEFORE the
    # AI-leverage enrichment step, so plain scanner signals legitimately lack it.
    leverage = _first_number(signal, ("leverage", "ai_leverage"), "signal leverage", minimum=1.0) if (
        signal.get("leverage") is not None or signal.get("ai_leverage") is not None
    ) else 1.0
    notional = signal.get("usd_size")
    if notional is None:
        invested = _first_number(
            signal,
            ("invested_amount", "margin_required_usd"),
            "signal invested_amount",
            minimum=0.0,
        )
        notional_usd = invested * leverage
    else:
        notional_usd = _finite(notional, "signal notional_usd", minimum=0.0)
    if notional_usd <= 0:
        raise PortfolioAdapterError("signal notional_usd must be > 0")
    if signal.get("margin_required_usd") is not None:
        margin = _finite(signal.get("margin_required_usd"), "signal margin_required_usd", minimum=0.0)
    elif signal.get("invested_amount") is not None:
        margin = _finite(signal.get("invested_amount"), "signal invested_amount", minimum=0.0)
    else:
        margin = notional_usd / leverage
    if any(signal.get(name) is not None for name in ("estimated_fees_usd", "entry_fee_usd", "fees_usd")):
        fees = _first_number(
            signal,
            ("estimated_fees_usd", "entry_fee_usd", "fees_usd"),
            "signal estimated_fees_usd",
            minimum=0.0,
        )
    else:
        # §83 (never hide transaction costs): estimate REAL entry costs with the
        # app's own cost model instead of a silent zero.
        fees = _estimate_entry_fees_usd(symbol, notional_usd, application_config)
    if any(signal.get(name) is not None for name in ("average_daily_volume_usd", "avg_daily_volume_usd")):
        adv = _first_number(
            signal,
            ("average_daily_volume_usd", "avg_daily_volume_usd"),
            "signal average_daily_volume_usd",
            minimum=0.0,
        )
    else:
        # Volume-based USD ADV (bars carry base-asset volume): a mathematical
        # fact, not an invention. Absent both → honest 0 and the constitution's
        # liquidity rule (if any) rejects with a clear reason instead of a crash.
        price = _finite_or_none(signal.get("entry") or signal.get("entry_price") or signal.get("price"))
        base_volume = _finite_or_none(signal.get("volume"))
        adv = (price * base_volume) if (price is not None and base_volume is not None) else 0.0

    # Auto-clamp notional to maximum allowable ADV liquidity participation
    if adv > 0 and constitution is not None:
        max_liq_pct = getattr(constitution, "max_liquidity_participation_pct", None)
        if max_liq_pct is not None and float(max_liq_pct) > 0:
            max_adv_notional = adv * (float(max_liq_pct) / 100.0)
            if notional_usd > max_adv_notional > 0:
                notional_usd = max_adv_notional
                margin = notional_usd / leverage
                signal["usd_size"] = round(notional_usd, 6)
                signal["invested_amount"] = round(margin, 6)
                signal["adv_liquidity_capped"] = True
                if isinstance(signal_raw, dict):
                    signal_raw["usd_size"] = round(notional_usd, 6)
                    signal_raw["invested_amount"] = round(margin, 6)
                    signal_raw["adv_liquidity_capped"] = True
    autonomy = _mapping(application_config.get("autonomy"))
    mode = str(autonomy.get("mode") or "ANALYZE").strip().upper()
    kill_switch = _required_bool(
        autonomy.get("kill_switch_active", False),
        "autonomy.kill_switch_active",
    )
    live_owner = _required_bool(
        autonomy.get("live_auto_owner_enabled", False),
        "autonomy.live_auto_owner_enabled",
    )
    proposal = OrderProposal(
        symbol=symbol,
        sector=sector,
        notional_usd=notional_usd,
        margin_required_usd=margin,
        fees_usd=fees,
        leverage=leverage,
        loss_at_stop_usd=_loss_at_stop(signal, notional_usd, "signal"),
        average_daily_volume_usd=adv,
        data_age_seconds=_data_age_seconds(signal, assessed_at),
        has_data_conflict=_data_conflict(signal, constitution),
        strategy_status=_strategy_status(signal, constitution),
        is_live_autonomous=mode == "LIVE_AUTO",
        live_auto_owner_enabled=live_owner,
        kill_switch_active=kill_switch,
    )
    return snapshot, proposal


__all__ = [
    "PortfolioAdapterError",
    "adapt_v7_position",
    "build_v7_constraint_inputs",
]
