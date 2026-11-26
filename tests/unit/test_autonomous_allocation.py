from types import SimpleNamespace

from core.agent_manager import (
    AgentManager,
    _autonomous_allocation_policy,
    _candidate_allocation_bucket,
)


def _config(*, fallback_when_empty=True):
    return {
        "autonomous_candidates_enabled": True,
        "autonomous_sources": {"scanner": True, "watchlist": True, "pre_earnings": True},
        "autonomous_allocation": {
            "enabled": True,
            "buckets_pct": {"stocks": 33, "crypto": 33, "pre_earnings": 33},
            "fallback_when_empty": fallback_when_empty,
        },
        "max_autonomous_candidates_per_cycle": 0,
        "max_autonomous_trades_per_cycle": 1,
        "autonomous_cooldown_minutes": 45,
        "autonomous_ticker_lockout_hours": 0,
        "autonomous_allow_scale_in": False,
    }


def _signal(ticker, *, source="scanner", strategy="SCANNER_AI_LONG", score=95):
    return {
        "id": f"signal-{ticker}-{source}",
        "ticker": ticker,
        "action": "BUY",
        "autonomous_source": source,
        "strategy_type": strategy,
        "signal_score": score,
    }


def _manager(monkeypatch, *, scanner, pre_earnings, fallback_when_empty=True):
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {"execution": SimpleNamespace(is_running=True)}
    manager._autonomous_last_decisions = {}
    manager._autonomous_source_stats = {}
    manager._autonomous_allocation_current = {}
    manager._autonomous_allocation_state_loaded = True
    manager.config_manager = SimpleNamespace(config={})
    monkeypatch.setattr(
        "core.agent_manager.record_autonomous_allocation_state",
        lambda **payload: dict(payload),
    )
    monkeypatch.setattr(manager, "_bot_mode_active", lambda: True)
    monkeypatch.setattr(manager, "_execution_config", lambda: _config(fallback_when_empty=fallback_when_empty))
    monkeypatch.setattr(manager, "_scanner_candidates", lambda cfg: [dict(item) for item in scanner])
    monkeypatch.setattr(manager, "_pre_earnings_candidates", lambda cfg: [dict(item) for item in pre_earnings])
    monkeypatch.setattr(manager, "_autonomous_trade_allowed_now", lambda signal: (True, ""))
    monkeypatch.setattr(manager, "_has_open_ticker_conflict", lambda signal, *, allow_scale_in: False)
    monkeypatch.setattr(manager, "_has_recent_close_lockout", lambda signal, hours: (False, ""))
    monkeypatch.setattr(manager, "_attach_candidate_price_integrity", lambda signal: None)
    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        lambda: SimpleNamespace(get_positions=lambda: []),
    )
    return manager


def test_candidate_allocation_bucket_distinguishes_all_three_lanes():
    assert _candidate_allocation_bucket(_signal("NVDA")) == "stocks"
    assert _candidate_allocation_bucket(_signal("BTC-USD")) == "crypto"
    assert _candidate_allocation_bucket(
        _signal("MDB", source="pre_earnings", strategy="PRE_EARNINGS_DIRECTIONAL")
    ) == "pre_earnings"


def test_equal_33_percent_weights_are_normalized_without_allocating_the_rounding_remainder():
    policy = _autonomous_allocation_policy(_config())

    assert policy["enabled"] is True
    assert policy["weights_pct"] == {"stocks": 33.0, "crypto": 33.0, "pre_earnings": 33.0}
    assert policy["normalized_pct"] == {
        "stocks": 33.3333,
        "crypto": 33.3333,
        "pre_earnings": 33.3333,
    }


def test_monitor_rotates_one_slot_across_stocks_crypto_and_pre_earnings(monkeypatch):
    manager = _manager(
        monkeypatch,
        scanner=[_signal("NVDA"), _signal("BTC-USD")],
        pre_earnings=[_signal("MDB", source="pre_earnings", strategy="PRE_EARNINGS_DIRECTIONAL")],
    )
    sent = []
    monkeypatch.setattr(manager, "on_new_signal", lambda signal: sent.append(signal))

    cycles = [manager._autonomous_monitor_once() for _ in range(3)]

    assert [item["ticker"] for item in sent] == ["NVDA", "BTC-USD", "MDB"]
    assert [cycle["allocation"]["target_bucket"] for cycle in cycles] == [
        "stocks",
        "crypto",
        "pre_earnings",
    ]
    assert all(cycle["submitted"] == 1 for cycle in cycles)
    assert all(cycle["allocation"]["fallback_used"] is False for cycle in cycles)
    assert cycles[0]["limits"] == {
        "candidate_reviews_per_cycle": 0,
        "trade_submissions_per_cycle": 1,
        "scanner_symbols_per_bucket": 25,
        "watchlist_symbols": 100,
    }
    assert [item["autonomous_allocation"]["bucket"] for item in sent] == [
        "stocks",
        "crypto",
        "pre_earnings",
    ]


def test_monitor_falls_back_when_the_target_bucket_has_no_candidate(monkeypatch):
    manager = _manager(monkeypatch, scanner=[_signal("BTC-USD")], pre_earnings=[])
    sent = []
    monkeypatch.setattr(manager, "on_new_signal", lambda signal: sent.append(signal))

    cycle = manager._autonomous_monitor_once()

    assert cycle["allocation"]["target_bucket"] == "stocks"
    assert cycle["allocation"]["actual_buckets"] == ["crypto"]
    assert cycle["allocation"]["fallback_used"] is True
    assert sent[0]["autonomous_allocation"]["fallback_used"] is True


def test_monitor_can_reserve_the_target_slot_without_fallback(monkeypatch):
    manager = _manager(
        monkeypatch,
        scanner=[_signal("BTC-USD")],
        pre_earnings=[],
        fallback_when_empty=False,
    )
    sent = []
    monkeypatch.setattr(manager, "on_new_signal", lambda signal: sent.append(signal))

    cycle = manager._autonomous_monitor_once()

    assert sent == []
    assert cycle["submitted"] == 0
    assert cycle["skipped"]["allocation_bucket"] == 1


def test_weighted_rotation_state_survives_manager_restart(monkeypatch):
    stored = {}

    def read_state():
        return dict(stored)

    def write_state(**payload):
        stored.clear()
        stored.update(payload)
        return dict(stored)

    monkeypatch.setattr("core.agent_manager.get_autonomous_allocation_state", read_state)
    monkeypatch.setattr("core.agent_manager.record_autonomous_allocation_state", write_state)
    monkeypatch.setattr("core.agent_manager.get_autonomous_cycle_status", lambda: {})
    policy = _autonomous_allocation_policy(_config())

    first = AgentManager.__new__(AgentManager)
    first._autonomous_allocation_current = {}
    first._autonomous_allocation_state_loaded = False
    assert first._next_autonomous_allocation_bucket(policy) == "stocks"

    restarted = AgentManager.__new__(AgentManager)
    restarted._autonomous_allocation_current = {}
    restarted._autonomous_allocation_state_loaded = False
    assert restarted._next_autonomous_allocation_bucket(policy) == "crypto"
