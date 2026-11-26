"""Fail-closed evidence bundle for automated Capital entries.

Signals remain useful research objects even when they are not ready to open a
Capital position.  This module is deliberately limited to automated execution:
manual terminal orders use their own explicit workflow.  An automated entry
must therefore prove four independent facts at the time it is evaluated:

* the order levels and source timestamp are internally coherent;
* the exact V2 strategy has a fresh, faithful out-of-sample validation;
* matching realised Capital trades still have a positive net edge; and
* current source-backed News has AI analysis and does not contradict direction.

The module never turns an exploratory or price-only backtest into an execution
approval.  Missing, stale, or malformed evidence blocks the entry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Dict, Iterable, Optional

from core.autonomous_trade_guard import (
    evaluate_autonomous_news,
    evaluate_autonomous_performance,
)
from core.market_hours import is_pre_earnings_entry_window
from core.strategy_v2.performance_cache import StrategyPerformanceCache


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _direction(signal: Dict[str, Any]) -> str:
    raw = _text((signal or {}).get("direction") or (signal or {}).get("action") or (signal or {}).get("type")).upper()
    if raw in {"BUY", "LONG"}:
        return "LONG"
    if raw in {"SELL", "SHORT"}:
        return "SHORT"
    return ""


def _parse_timestamp(value: Any) -> Optional[datetime]:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _asset_class(signal: Dict[str, Any]) -> str:
    explicit = _text((signal or {}).get("asset_class")).upper()
    if explicit:
        return explicit
    asset_type = _text((signal or {}).get("asset_type")).lower()
    if asset_type == "crypto":
        return "CRYPTO_FUTURES"
    if asset_type == "forex":
        return "FOREX_MAJORS"
    if asset_type == "commodity":
        return "COMMODITIES"
    return "US_STOCKS"


def _execution_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Accept either the execution subsection or the complete app config.

    AgentManager and ExecutionAgent pass ``agents.execution`` directly, while
    the AI validation pipeline owns the complete application configuration.
    Keeping this normalization here makes every defence-in-depth invocation
    evaluate the exact same mode and gate policy.
    """
    cfg = dict(config or {})
    agents = cfg.get("agents")
    if isinstance(agents, dict) and isinstance(agents.get("execution"), dict):
        return dict(agents["execution"])
    return cfg


def _gate_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = _execution_config(config)
    raw = cfg.get("capital_entry_gate")
    return dict(raw) if isinstance(raw, dict) else {"enabled": True}


def _with_gate_config(config: Optional[Dict[str, Any]], key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    merged = _execution_config(config)
    merged[key] = dict(value or {})
    return merged


def _is_paper_s7_capital_experiment(signal: Dict[str, Any], config: Optional[Dict[str, Any]]) -> bool:
    """Allow an explicitly configured unvalidated strategy only in paper mode.

    This is intentionally narrower than disabling the Capital gate: it accepts
    just the whitelisted strategy hypotheses in the simulated portfolio, keeps the
    entry-data and AI checks, and fails closed for any non-paper mode.
    """
    execution = _execution_config(config)
    paper_cfg = execution.get("paper_capital") if isinstance(execution.get("paper_capital"), dict) else {}
    if _text(execution.get("mode")).lower() != "paper":
        return False
    allow_unvalidated = bool(
        paper_cfg.get("allow_unvalidated_strategies", paper_cfg.get("allow_unvalidated_s7", False))
    )
    if not bool(paper_cfg.get("enabled", False)) or not allow_unvalidated:
        return False
    strategy_source = _text((signal or {}).get("strategy_source")).upper()
    strategy_id = _text((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id")).upper()
    allowed_ids = paper_cfg.get(
        "strategy_ids", ["S7_LIQUIDITY_SWEEP_REVERSAL", "S8_SUPERTREND_BOS"]
    )
    allowed_sources = paper_cfg.get("strategy_sources", ["V2"])
    allowed = {
        _text(value).upper()
        for value in (allowed_ids if isinstance(allowed_ids, list) else [])
        if _text(value)
    }
    sources = {
        _text(value).upper()
        for value in (allowed_sources if isinstance(allowed_sources, list) else [])
        if _text(value)
    }
    return strategy_source in sources and strategy_id in allowed and (
        bool((signal or {}).get("research_only"))
        or bool((signal or {}).get("paper_capital_experiment"))
    )


def _is_pre_earnings_candidate(signal: Dict[str, Any]) -> bool:
    """Return True if the candidate is generated by Pre-Earnings autonomous discovery."""
    payload = signal or {}
    return (
        _text(payload.get("autonomous_source")).lower() == "pre_earnings"
        or _text(payload.get("signal_mode")).upper() == "AUTONOMOUS_PRE_EARNINGS"
        or _text(payload.get("strategy_type")).upper().startswith("PRE_EARNINGS_")
        or _text(payload.get("strategy_id")).upper().startswith("PRE_EARNINGS_")
    )


def _paper_autonomous_experiment_source(
    signal: Dict[str, Any],
    config: Optional[Dict[str, Any]],
) -> str:
    """Identify price-first autonomous candidates admitted only to paper Capital.

    Scanner and Pre-Earnings candidates are prospective strategies by design, so
    they cannot already have the faithful V2/OOS history that the normal Capital
    path requires.  In paper mode they collect that history behind the unchanged
    data, Risk Constitution, AI, sizing, and broker gates.  Live mode never gets
    this exception.
    """
    execution = _execution_config(config)
    if _text(execution.get("mode")).lower() != "paper":
        return ""

    payload = signal or {}
    source = _text(payload.get("autonomous_source")).lower()
    signal_mode = _text(payload.get("signal_mode")).upper()
    strategy_id = _text(
        payload.get("strategy_type") or payload.get("strategy_id")
    ).upper()
    if (
        source == "pre_earnings"
        or signal_mode == "AUTONOMOUS_PRE_EARNINGS"
        or strategy_id.startswith("PRE_EARNINGS_")
    ):
        return "pre_earnings"
    if (
        source == "scanner"
        or signal_mode == "AUTONOMOUS_SCANNER"
        or strategy_id.startswith("SCANNER_AI_")
    ):
        return "scanner"
    # Event rules (a Binance delisting notice) are prospective experiments too.
    if (
        source == "delist_notice"
        or signal_mode == "AUTONOMOUS_EVENT"
        or strategy_id.startswith("DELIST_NOTICE_")
    ):
        return "event"
    # Learning mode: trades Opus chose on a mover or a headline (owner, 2026-11-24).
    if (
        source == "learning"
        or signal_mode == "AUTONOMOUS_LEARNING"
        or strategy_id.startswith(("AI_MOVER_", "AI_NEWS_"))
    ):
        return "learning"
    # Factor rules (the weekly cross-sectional momentum basket, Strategy Lab rules) likewise.
    if (
        source in ("xs_momentum_basket", "strategy_lab")
        or signal_mode == "AUTONOMOUS_FACTOR"
        or strategy_id.startswith(("XS_MOMENTUM_", "LAB_"))
    ):
        return "factor"
    return ""


def capital_entry_requires_ai_review(config: Optional[Dict[str, Any]]) -> bool:
    gate = _gate_config(config)
    return bool(gate.get("enabled", True) and gate.get("require_ai_review", True))


def evaluate_entry_data(signal: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate order levels and reject stale automated signal payloads."""
    gate = _gate_config(config)
    enabled = bool(gate.get("enabled", True))
    data_cfg = gate.get("data") if isinstance(gate.get("data"), dict) else {}
    direction = _direction(signal)
    entry = _number((signal or {}).get("entry") or (signal or {}).get("entry_price") or (signal or {}).get("price"))
    target = _number((signal or {}).get("tp") or (signal or {}).get("take_profit_price") or (signal or {}).get("take_profit"))
    stop = _number((signal or {}).get("sl") or (signal or {}).get("stop_loss_price") or (signal or {}).get("stop_loss"))

    if entry > 0:
        is_short = direction == "SHORT"
        if target <= 0:
            tp_pct = _number((signal or {}).get("tp_pct") or (signal or {}).get("take_profit_pct"))
            if tp_pct > 0:
                frac = tp_pct / 100.0 if tp_pct > 1.0 else tp_pct
                target = entry * (1 - frac) if is_short else entry * (1 + frac)
        if stop <= 0:
            sl_pct = _number((signal or {}).get("sl_pct") or (signal or {}).get("stop_loss_pct"))
            if sl_pct > 0:
                frac = sl_pct / 100.0 if sl_pct > 1.0 else sl_pct
                stop = entry * (1 + frac) if is_short else entry * (1 - frac)

    created_at = (signal or {}).get("created_at") or (signal or {}).get("timestamp")
    created = _parse_timestamp(created_at)
    max_age = max(1, int(_number(data_cfg.get("max_signal_age_seconds"), 300)))
    # The configurable threshold may be relaxed for paper operation, but an
    # automated entry must still retain a positive reward/risk profile.
    min_rr = max(1.1, _number(data_cfg.get("min_risk_reward_ratio"), 1.2))
    require_price_integrity = bool(data_cfg.get("require_price_integrity", True))
    price_integrity = (signal or {}).get("price_integrity")
    price_verified = bool((signal or {}).get("price_verified", False))
    base = {
        "gate": "capital_entry_data",
        "enabled": enabled,
        "direction": direction,
        "entry": entry,
        "target": target,
        "stop": stop,
        "created_at": _text(created_at),
        "max_signal_age_seconds": max_age,
        "min_risk_reward_ratio": min_rr,
        "require_price_integrity": require_price_integrity,
    }
    if not enabled:
        return {**base, "allowed": True, "status": "disabled", "reason": "Capital entry gate disabled by configuration"}
    if not direction or not all(isfinite(value) and value > 0 for value in (entry, target, stop)):
        return {**base, "allowed": False, "status": "invalid_levels", "reason": "Direction, entry, target, and stop must be finite positive values"}
    if require_price_integrity:
        integrity_status = _text((price_integrity or {}).get("status")).lower() if isinstance(price_integrity, dict) else "missing"
        integrity_verified = bool((price_integrity or {}).get("is_verified", False)) if isinstance(price_integrity, dict) else False
        if not price_verified or not integrity_verified or integrity_status != "verified":
            return {
                **base,
                "allowed": False,
                "status": "unverified_price_integrity",
                "reason": "Automated Capital entry requires concordant live and technical price evidence",
                "price_integrity": dict(price_integrity or {}) if isinstance(price_integrity, dict) else {},
            }
    if (direction == "LONG" and not (stop < entry < target)) or (direction == "SHORT" and not (target < entry < stop)):
        return {**base, "allowed": False, "status": "invalid_levels", "reason": "Target and stop are inconsistent with trade direction"}
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = reward / risk if risk > 0 else 0.0
    if rr < min_rr:
        return {**base, "allowed": False, "status": "risk_reward_too_low", "reason": f"Risk/reward {rr:.2f} is below required {min_rr:.2f}", "risk_reward_ratio": round(rr, 6)}
    if created is None:
        return {**base, "allowed": False, "status": "missing_timestamp", "reason": "Automated entry has no parseable source timestamp"}
    age_seconds = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
    if age_seconds > max_age:
        return {**base, "allowed": False, "status": "stale_signal", "reason": f"Signal age {age_seconds:.0f}s exceeds {max_age}s", "age_seconds": round(age_seconds, 3), "risk_reward_ratio": round(rr, 6)}
    return {**base, "allowed": True, "status": "valid", "reason": "Levels and signal timestamp are valid", "age_seconds": round(age_seconds, 3), "risk_reward_ratio": round(rr, 6)}


def evaluate_backtest_validation(
    signal: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    *,
    perf_cache: Optional[StrategyPerformanceCache] = None,
) -> Dict[str, Any]:
    """Require a fresh, faithful OOS validation for the exact V2 strategy."""
    gate = _gate_config(config)
    enabled = bool(gate.get("enabled", True))
    backtest_cfg = gate.get("backtest") if isinstance(gate.get("backtest"), dict) else {}
    strategy_source = _text((signal or {}).get("strategy_source") or "V1").upper()
    ticker = _text((signal or {}).get("ticker")).upper()
    strategy_type = _text((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id")).upper()
    direction = _direction(signal)
    asset_class = _asset_class(signal)
    required_method = _text(backtest_cfg.get("required_validation_method") or "faithful_v2_walk_forward").lower()
    min_sample = max(1, int(_number(backtest_cfg.get("min_sample"), 30)))
    min_pf = max(0.0, _number(backtest_cfg.get("min_profit_factor"), 1.2))
    min_expectancy = _number(backtest_cfg.get("min_expectancy_r"), 0.0)
    max_drawdown = max(0.0, _number(backtest_cfg.get("max_drawdown_pct"), 25.0))
    ttl_default = max(60, int(_number(backtest_cfg.get("max_age_seconds"), 21600)))
    ttl_by_asset = backtest_cfg.get("max_age_seconds_by_asset") if isinstance(backtest_cfg.get("max_age_seconds_by_asset"), dict) else {}
    ttl = max(60, int(_number(ttl_by_asset.get(asset_class), ttl_default)))
    base = {
        "gate": "capital_entry_backtest",
        "enabled": enabled,
        "ticker": ticker,
        "asset_class": asset_class,
        "strategy_source": strategy_source,
        "strategy_type": strategy_type,
        "direction": direction,
        "thresholds": {
            "required_validation_method": required_method,
            "min_sample": min_sample,
            "min_profit_factor": min_pf,
            "min_expectancy_r": min_expectancy,
            "max_drawdown_pct": max_drawdown,
            "max_age_seconds": ttl,
        },
    }
    if not enabled:
        return {**base, "allowed": True, "status": "disabled", "reason": "Capital entry gate disabled by configuration"}
    if strategy_source != "V2":
        return {**base, "allowed": False, "status": "unfaithful_strategy_source", "reason": "Automatic Capital execution requires a faithful V2 strategy validation"}
    if not ticker or not strategy_type or not direction:
        return {**base, "allowed": False, "status": "invalid_strategy_identity", "reason": "Ticker, V2 strategy type, and direction are required"}
    cache = perf_cache or StrategyPerformanceCache()
    performance = cache.get_entry(ticker, asset_class, strategy_type, direction)
    if not performance:
        return {**base, "allowed": False, "status": "missing_validation", "reason": "No matching out-of-sample validation is recorded"}
    validation_status = _text(performance.get("validation_status")).lower()
    validation_method = _text(performance.get("validation_method")).lower()
    validation_at = _parse_timestamp(performance.get("validation_as_of") or performance.get("last_run"))
    metrics = {
        "sample_size": int(_number(performance.get("sample_size"), 0)),
        "profit_factor": performance.get("profit_factor"),
        "expectancy_r": _number(performance.get("expectancy_R", performance.get("expectancy_r")), 0.0),
        "max_drawdown_pct": _number(performance.get("max_drawdown"), 0.0),
        "validation_status": validation_status or "missing",
        "validation_method": validation_method or "missing",
        "validation_as_of": _text(performance.get("validation_as_of") or performance.get("last_run")),
        "costs_used": performance.get("costs_used") if isinstance(performance.get("costs_used"), dict) else {},
    }
    if validation_status != "validated":
        return {**base, "allowed": False, "status": "unverified_validation", "reason": f"Validation status is {validation_status or 'missing'}, not validated", "metrics": metrics}
    if validation_method != required_method:
        return {**base, "allowed": False, "status": "validation_method_mismatch", "reason": f"Validation method {validation_method or 'missing'} is not {required_method}", "metrics": metrics}
    if validation_at is None:
        return {**base, "allowed": False, "status": "missing_validation_timestamp", "reason": "Validation has no parseable as-of timestamp", "metrics": metrics}
    validation_age = max(0.0, (datetime.now(timezone.utc) - validation_at).total_seconds())
    metrics["validation_age_seconds"] = round(validation_age, 3)
    if validation_age > ttl:
        return {**base, "allowed": False, "status": "stale_validation", "reason": f"Validation age {validation_age:.0f}s exceeds {ttl}s", "metrics": metrics}
    costs = metrics["costs_used"]
    if not costs or any(_number(value, -1.0) < 0.0 for value in costs.values()):
        return {**base, "allowed": False, "status": "invalid_cost_model", "reason": "Validation has no complete non-negative cost model", "metrics": metrics}
    pf_raw = metrics["profit_factor"]
    pf = None if pf_raw is None else _number(pf_raw, 0.0)
    if metrics["sample_size"] < min_sample:
        return {**base, "allowed": False, "status": "insufficient_sample", "reason": f"Validation sample {metrics['sample_size']} is below {min_sample}", "metrics": metrics}
    if pf is None or pf < min_pf:
        return {**base, "allowed": False, "status": "negative_edge", "reason": f"Validation profit factor {pf if pf is not None else 'unmeasurable'} is below {min_pf:.2f}", "metrics": metrics}
    if metrics["expectancy_r"] <= min_expectancy:
        return {**base, "allowed": False, "status": "negative_edge", "reason": f"Validation expectancy R {metrics['expectancy_r']:.3f} is not above {min_expectancy:.3f}", "metrics": metrics}
    if metrics["max_drawdown_pct"] > max_drawdown:
        return {**base, "allowed": False, "status": "drawdown_exceeded", "reason": f"Validation drawdown {metrics['max_drawdown_pct']:.2f}% exceeds {max_drawdown:.2f}%", "metrics": metrics}
    return {**base, "allowed": True, "status": "validated", "reason": "Fresh faithful out-of-sample validation satisfies thresholds", "metrics": metrics}


DEFAULT_PAPER_EXPERIMENT_MAX_SAMPLE = 100


def _evidence_positions(
    positions: Iterable[Dict[str, Any]],
    signal: Dict[str, Any],
) -> tuple[list, Dict[str, Any]]:
    """Closed positions that count as strategy evidence, plus what was left out.

    A position marked ``evidence_excluded`` (its exit was delayed by an outage)
    is a real paper result but not a measurement of the strategy: it is not
    part of the realised-edge sample or of the stopping-rule sample. The
    summary (count, total P&L of the excluded trades of this strategy and
    direction) is attached to the result so nothing is hidden.
    """
    from core.portfolio.evidence import excluded_trades_summary, split_evidence

    counted, excluded = split_evidence(list(positions or []))
    strategy_type = _text((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id")).upper()
    direction = _direction(signal or {})
    same_setup = [
        position for position in excluded
        if _text(position.get("strategy_type")).upper() == strategy_type
        and (not direction or _text(position.get("direction")).upper() == direction)
    ]
    summary = excluded_trades_summary(same_setup)
    summary["scope"] = "closed positions of the same strategy and direction"
    return counted, summary


def evaluate_paper_experiment_budget(
    signal: Dict[str, Any],
    positions: Iterable[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stopping rule for paper Capital experiments.

    A prospective strategy is admitted to paper Capital without OOS history so
    it can earn one.  That admission is a budget, not a standing exemption:
    once the same strategy, direction and entry regime has closed
    ``max_sample_without_edge`` trades and its realised record still fails the
    profitability thresholds, the experiment has its answer and stops opening
    positions.  A new entry regime (``entry_strategy``) starts a fresh budget.
    """
    gate = _gate_config(config)
    experiment_cfg = gate.get("paper_experiment") if isinstance(gate.get("paper_experiment"), dict) else {}
    max_sample = max(1, int(_number(
        experiment_cfg.get("max_sample_without_edge"), DEFAULT_PAPER_EXPERIMENT_MAX_SAMPLE,
    )))
    base = {"gate": "paper_experiment_budget", "max_sample_without_edge": max_sample}
    if not bool(experiment_cfg.get("enabled", True)):
        return {**base, "allowed": True, "status": "disabled", "reason": "Paper experiment budget disabled by configuration"}

    realized_cfg = dict(gate.get("realized_performance")) if isinstance(gate.get("realized_performance"), dict) else {}
    realized_cfg["enabled"] = True
    realized_signal = dict(signal or {})
    realized_signal["_autonomous_candidate"] = True
    evidence_positions, excluded_trades = _evidence_positions(positions, signal)
    realized = evaluate_autonomous_performance(
        realized_signal,
        evidence_positions,
        _with_gate_config(config, "autonomous_profitability_gate", realized_cfg),
    )
    metrics = realized.get("metrics") if isinstance(realized.get("metrics"), dict) else {}
    sample = int(_number(metrics.get("sample_size"), 0))
    base = {**base, "sample_size": sample, "realized": realized, "excluded_trades": excluded_trades}
    if realized.get("status") in {"negative_edge", "drawdown_exceeded"} and sample >= max_sample:
        return {
            **base,
            "allowed": False,
            "status": "experiment_concluded_no_edge",
            "reason": (
                f"Paper experiment concluded after {sample} closed trades without a net edge "
                f"({realized.get('reason')}); change the entry regime to start a new experiment"
            ),
        }
    return {
        **base,
        "allowed": True,
        "status": "validated" if realized.get("status") == "validated" else "collecting_evidence",
        "reason": f"{sample} of {max_sample} budgeted paper trades closed",
    }


def evaluate_capital_entry(
    signal: Dict[str, Any],
    positions: Iterable[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    *,
    perf_cache: Optional[StrategyPerformanceCache] = None,
    news_pulse: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the complete, ordered evidence bundle for an automated entry."""
    gate = _gate_config(config)
    enabled = bool(gate.get("enabled", True))
    checks: Dict[str, Dict[str, Any]] = {}
    paper_s7_experiment = _is_paper_s7_capital_experiment(signal, config)
    paper_autonomous_source = _paper_autonomous_experiment_source(signal, config)
    # Research visibility is intentionally independent from Capital execution.
    # Keep this guard ahead of the configurable gate so an accidental disabled
    # Capital gate can never promote an unvalidated observational S7 signal.
    if bool((signal or {}).get("research_only")) and not paper_s7_experiment:
        reason = _text((signal or {}).get("capital_execution_block_reason")) or (
            "Research-only signal is blocked from Capital until faithful out-of-sample validation passes"
        )
        checks["research_only"] = {
            "allowed": False,
            "status": "research_only",
            "reason": reason,
        }
        return {
            "gate": "capital_entry",
            "enabled": enabled,
            "allowed": False,
            "check": "research_only",
            "status": "research_only",
            "reason": reason,
            "checks": checks,
        }
    if not enabled:
        return {
            "gate": "capital_entry",
            "enabled": False,
            "allowed": True,
            "check": "disabled",
            "status": "disabled",
            "reason": "Capital entry gate disabled by configuration",
            "checks": checks,
        }
    data = evaluate_entry_data(signal, config)
    checks["data"] = data
    if not data.get("allowed"):
        return {"gate": "capital_entry", "enabled": enabled, "allowed": False, "check": "data", "status": data.get("status"), "reason": data.get("reason"), "checks": checks}

    if paper_s7_experiment:
        strategy_id = _text((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id")).upper()
        checks["paper_s7_experiment"] = {
            "allowed": True,
            "status": "paper_only",
            "reason": f"Unvalidated {strategy_id} is admitted to the simulated Capital portfolio only; OOS gates remain bypassed solely in paper mode",
        }
        return {
            "gate": "capital_entry",
            "enabled": enabled,
            "allowed": True,
            "check": "paper_s7_experiment",
            "status": "paper_only",
            "reason": f"{strategy_id} paper Capital experiment passed data integrity; no live execution is permitted",
            "checks": checks,
            "paper_capital_experiment": True,
        }

    # A rule basket is two legs of one rule. The budget judges one strategy type and direction at a time,
    # so it would stop one leg and leave a one-sided basket. The Strategy Lab retires its rules itself (on
    # the evidence of both legs); the weekly momentum basket has its own pooled stopping rule.
    # Learning mode runs toward 10,000 closed trades (owner, 2026-11-24): no 100-trade stop either.
    if paper_autonomous_source and paper_autonomous_source not in ("factor", "learning"):
        budget = evaluate_paper_experiment_budget(signal, positions, config)
        checks["paper_experiment_budget"] = budget
        if not budget.get("allowed"):
            return {"gate": "capital_entry", "enabled": enabled, "allowed": False, "check": "paper_experiment_budget", "status": budget.get("status"), "reason": budget.get("reason"), "checks": checks}

    if paper_autonomous_source == "pre_earnings":
        pe_ctx =signal.get("pre_earnings_context") if isinstance(signal.get("pre_earnings_context"), dict) else signal
        earnings_time_utc = pe_ctx.get("earnings_time_utc") or signal.get("earnings_time_utc")
        session = pe_ctx.get("session") or signal.get("session")
        # 2026-09-03 audit P2: read the SAME window key the Pre-Earnings agent
        # and the UI badge use (pre_earnings.entry_window_hours) so the badge
        # shown in the UI and the gate that blocks the order can never diverge.
        try:
            _root = config if isinstance(config, dict) else getattr(config, "config", {}) or {}
            _window_hours = float(
                ((_root.get("pre_earnings") or {}).get("entry_window_hours")) or 2.0
            )
        except (TypeError, ValueError, AttributeError):
            _window_hours = 2.0
        window_open, window_reason, window_meta = is_pre_earnings_entry_window(
            earnings_time_utc, session, window_hours=_window_hours
        )
        checks["pre_earnings_window"] = {
            "allowed": window_open,
            "status": "window_open" if window_open else "outside_pre_earnings_window",
            "reason": window_reason,
            "entry_window": window_meta,
        }
        if not window_open:
            return {
                "gate": "capital_entry",
                "enabled": enabled,
                "allowed": False,
                "check": "pre_earnings_window",
                "status": "outside_pre_earnings_window",
                "reason": window_reason,
                "checks": checks,
            }

        strategy_id = _text((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id") or "PRE_EARNINGS").upper()
        checks["pre_earnings"] = {
            "allowed": True,
            "status": "paper_admitted",
            "reason": f"Pre-Earnings {strategy_id} candidate passed data integrity and 2h pre-close entry window",
            "entry_window": window_meta,
        }
        return {
            "gate": "capital_entry",
            "enabled": enabled,
            "allowed": True,
            "check": "pre_earnings",
            "status": "paper_admitted",
            "reason": f"Pre-Earnings {strategy_id} passed data integrity and 2h pre-close entry window",
            "checks": checks,
            "paper_capital_experiment": True,
        }

    if paper_autonomous_source in ("scanner", "event", "factor", "learning"):
        origin = {"scanner": "Scanner", "event": "Event rule", "learning": "Learning-mode"}.get(
            paper_autonomous_source, "Factor rule"
        )
        strategy_id = _text(
            (signal or {}).get("strategy_type")
            or (signal or {}).get("strategy_id")
            or "SCANNER"
        ).upper()
        checks["paper_autonomous_experiment"] = {
            "allowed": True,
            "status": "paper_only",
            "reason": (
                f"{origin} {strategy_id} candidate passed price integrity and is "
                "admitted only to the simulated Capital portfolio"
            ),
        }
        return {
            "gate": "capital_entry",
            "enabled": enabled,
            "allowed": True,
            "check": "paper_autonomous_experiment",
            "status": "paper_only",
            "reason": (
                f"{origin} {strategy_id} passed data integrity; prospective edge "
                "will be measured from paper outcomes"
            ),
            "checks": checks,
            "paper_capital_experiment": True,
        }

    backtest = evaluate_backtest_validation(signal, config, perf_cache=perf_cache)
    checks["backtest"] = backtest
    if not backtest.get("allowed"):
        return {"gate": "capital_entry", "enabled": enabled, "allowed": False, "check": "backtest", "status": backtest.get("status"), "reason": backtest.get("reason"), "checks": checks}

    realized_cfg = gate.get("realized_performance") if isinstance(gate.get("realized_performance"), dict) else {}
    realized_signal = dict(signal or {})
    # The legacy helper contains the exact matching and drawdown calculations.
    # Mark only this local copy as automated so manual terminal orders stay out
    # of the evidence path while every ExecutionAgent call is held to it.
    realized_signal["_autonomous_candidate"] = True
    evidence_positions, excluded_trades = _evidence_positions(positions, signal)
    realized = evaluate_autonomous_performance(
        realized_signal,
        evidence_positions,
        _with_gate_config(config, "autonomous_profitability_gate", realized_cfg),
    )
    realized["gate"] = "capital_entry_realized_performance"
    realized["excluded_trades"] = excluded_trades
    checks["realized_performance"] = realized
    if not realized.get("allowed"):
        return {"gate": "capital_entry", "enabled": enabled, "allowed": False, "check": "realized_performance", "status": realized.get("status"), "reason": realized.get("reason"), "checks": checks}

    news_cfg = gate.get("news") if isinstance(gate.get("news"), dict) else {}
    news_signal = dict(signal or {})
    news_signal["_autonomous_candidate"] = True
    news = evaluate_autonomous_news(
        news_signal,
        _with_gate_config(config, "autonomous_news_gate", news_cfg),
        news_pulse=news_pulse,
    )
    news["gate"] = "capital_entry_news"
    checks["news"] = news
    if not news.get("allowed"):
        return {"gate": "capital_entry", "enabled": enabled, "allowed": False, "check": "news", "status": news.get("status"), "reason": news.get("reason"), "checks": checks}
    return {"gate": "capital_entry", "enabled": enabled, "allowed": True, "check": "complete", "status": "validated", "reason": "Data, faithful OOS validation, realised edge, and AI News checks passed", "checks": checks}
