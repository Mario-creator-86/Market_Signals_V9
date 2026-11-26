"""Wave 3 (2026-11-15) — decision path: what turns analysis into paper entries.

Every rule here may only block or disclose more; none lets the bot open more
or larger positions than before.

1. Trade Radar: a negative backtest is a readiness blocker.
2. Tokenised stocks / stablecoin pairs are not autonomous entries.
3. Critic: a model veto is overridden only by a MEASURED score; minimum AI
   confidence for non-pre-earnings entries.
4. "Risk per trade (%)": the legacy key is the base position NOTIONAL.
5. Bot controller: a failed persist of START is taken back; a failed persist
   of STOP stays stopped and is reported as not durable.
6. Watchlist quick AI: bounded timeout, explicit unavailable state, price date.
7. Wording ("not a probability") and the direction-score input share cap.
8. Engine process reads cached news only.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

import core.agent_manager as am_mod  # noqa: E402
from agents.risk_agent import RiskAgent, resolve_position_limits  # noqa: E402
from agents.trade_radar_agent import TradeRadarAgent  # noqa: E402
from core.agent_manager import (  # noqa: E402
    AgentManager,
    _ai_min_entry_confidence_gate,
    _attach_cached_news_context_for_ai,
    _trade_radar_opportunity,
)
from core.ai.critic import _validated_quality_score, run_critic  # noqa: E402
from core.ai.decision import Action, Decision  # noqa: E402
from core.ai.provider import AIResponse  # noqa: E402
from core.autonomous_candidates import (  # noqa: E402
    autonomous_instrument_exclusion,
    build_scanner_candidate_signal,
    instrument_exclusion_policy,
    instrument_tags_from_scanner_state,
)
from core.bot_controller import BotController  # noqa: E402
from core.pre_earnings.score import (  # noqa: E402
    DIRECTION_INPUT_MAX_SHARE_MULTIPLE,
    cap_direction_input_shares,
)


# ---------------------------------------------------------------------------
# 1. Trade Radar — negative backtest
# ---------------------------------------------------------------------------

def _bt(trades, pf, available=True):
    return {"available": available, "closed_trades": trades, "profit_factor": pf, "win_rate": 26.0, "days": 21}


def test_radar_backtest_negative_needs_enough_trades_and_pf_below_one():
    cfg = {"backtest_negative_min_trades": 20}
    negative = TradeRadarAgent._annotate_backtest_verdict(_bt(27, 0.64), cfg)
    assert negative["negative"] is True and negative["thin_sample"] is False
    assert negative["negative_min_trades"] == 20

    thin = TradeRadarAgent._annotate_backtest_verdict(_bt(9, 0.64), cfg)
    assert thin["negative"] is False and thin["thin_sample"] is True

    positive = TradeRadarAgent._annotate_backtest_verdict(_bt(27, 1.31), cfg)
    assert positive["negative"] is False and positive["thin_sample"] is False

    # Unmeasurable profit factor (no losing trade yet) and an unavailable
    # backtest are neither negative nor thin.
    unmeasured = TradeRadarAgent._annotate_backtest_verdict(_bt(27, None), cfg)
    assert unmeasured["negative"] is False and unmeasured["thin_sample"] is False
    unavailable = TradeRadarAgent._annotate_backtest_verdict(_bt(27, 0.0, available=False), cfg)
    assert unavailable["negative"] is False and unavailable["thin_sample"] is False


def test_radar_backtest_negative_threshold_is_configurable_and_defaults_to_20():
    assert TradeRadarAgent.DEFAULT_CONFIG["backtest_negative_min_trades"] == 20
    cfg = TradeRadarAgent._sanitize_config({}, {})
    assert cfg["backtest_negative_min_trades"] == 20
    cfg = TradeRadarAgent._sanitize_config({}, {"backtest_negative_min_trades": 5})
    assert cfg["backtest_negative_min_trades"] == 5
    assert TradeRadarAgent._annotate_backtest_verdict(_bt(9, 0.64), cfg)["negative"] is True


def test_radar_negative_backtest_blocks_readiness_that_was_ready():
    """Live: "READY · score 62" with "BT 21d: WR 26% · PF 0.64 · 27 trades"."""
    pack = TradeRadarAgent._annotate_backtest_verdict(_bt(27, 0.64), {})
    news = {"available": True, "checked": True}
    calendar = {"available": True, "checked": True}
    flags = TradeRadarAgent._build_red_flags("LONG", news, calendar, pack, 10.0, 12.0)
    assert "backtest_negative" in flags
    assert "backtest_negative" in TradeRadarAgent.HARD_BLOCKERS

    ready_before = TradeRadarAgent._build_readiness("LONG", 62.0, 10.0, 11.0, 9.5, [])
    assert ready_before["status"] == "READY" and ready_before["can_open"] is True

    readiness = TradeRadarAgent._build_readiness("LONG", 62.0, 10.0, 11.0, 9.5, flags)
    assert readiness["status"] == "BLOCKED"
    assert readiness["can_open"] is False
    assert readiness["close_to_open"] is False
    assert readiness["blockers"][0] == "backtest_negative"
    assert readiness["checks"]["no_hard_blockers"] is False
    assert "backtest" in readiness["note"].lower()


def test_radar_thin_losing_sample_is_a_note_not_a_blocker():
    pack = TradeRadarAgent._annotate_backtest_verdict(_bt(6, 0.64), {})
    flags = TradeRadarAgent._build_red_flags(
        "LONG", {"available": True}, {"available": True, "checked": True}, pack, 10.0, 12.0
    )
    assert "backtest_thin_sample" in flags and "backtest_negative" not in flags
    readiness = TradeRadarAgent._build_readiness("LONG", 62.0, 10.0, 11.0, 9.5, flags)
    assert readiness["status"] == "READY" and readiness["can_open"] is True
    assert readiness["checks"]["no_hard_blockers"] is True
    # The card reads the note from the blockers list; it is declared non-blocking.
    assert readiness["blockers"] == ["backtest_thin_sample"]
    assert readiness["non_blocking_notes"] == ["backtest_thin_sample"]


def test_blocked_radar_candidate_gives_no_opportunity_to_the_entry_path():
    blocked = [{
        "symbol": "SPCX",
        "analysis": {"confidence": 62},
        "readiness": {"status": "BLOCKED", "can_open": False, "blockers": ["backtest_negative"]},
    }]
    opportunity, reason = _trade_radar_opportunity(blocked, "SPCX")
    assert opportunity == 0.0
    assert reason == "trade_radar_blocked=backtest_negative"
    # Not reported as "radar unavailable": the confidence gate must read it as
    # a radar rejection, not as a missing opinion.
    assert "ticker_not_in_trade_radar" not in reason

    noted = [{
        "symbol": "SPCX",
        "analysis": {"confidence": 62},
        "readiness": {
            "status": "READY", "can_open": True,
            "blockers": ["backtest_thin_sample"], "non_blocking_notes": ["backtest_thin_sample"],
        },
    }]
    opportunity, reason = _trade_radar_opportunity(noted, "SPCX")
    assert opportunity == 62.0 and reason == "trade_radar_opportunity=62.0"


# ---------------------------------------------------------------------------
# 2. Tokenised stocks and stablecoins
# ---------------------------------------------------------------------------

_SCANNER_CFG = {
    "scanner_min_abs_change_pct": 5.0,
    "scanner_entry_mode": "fade",
    "scanner_fade_min_move_pct": 6.0,
    "scanner_fade_max_move_pct": 40.0,
    "scanner_min_score": 78.0,
}


def test_exclusion_policy_defaults_to_excluding_both_kinds():
    assert instrument_exclusion_policy(None) == {"exclude_tokenized_stocks": True, "exclude_stablecoins": True}
    assert instrument_exclusion_policy({}) == {"exclude_tokenized_stocks": True, "exclude_stablecoins": True}
    off = instrument_exclusion_policy({"autonomous": {"exclude_tokenized_stocks": False}})
    assert off == {"exclude_tokenized_stocks": False, "exclude_stablecoins": True}


def test_tagged_scanner_row_is_excluded_with_a_named_reason():
    row = {
        "symbol": "NVDABUSDT", "price": 140.0, "change_pct": -9.0, "volume": 5_000_000.0,
        "instrument_type": "tokenized_stock", "underlying": "NVDA", "is_tokenized_stock": True,
    }
    signal = build_scanner_candidate_signal(row, _SCANNER_CFG)
    assert signal is not None
    assert signal["instrument_type"] == "tokenized_stock" and signal["underlying"] == "NVDA"

    exclusion = autonomous_instrument_exclusion(signal)
    assert exclusion["reason"] == "tokenized_stock_excluded"
    assert exclusion["underlying"] == "NVDA"
    assert exclusion["config_key"] == "autonomous.exclude_tokenized_stocks"
    assert "NVDA" in exclusion["detail"]

    # Switched off by the owner: the candidate proceeds as before.
    assert autonomous_instrument_exclusion(signal, {"exclude_tokenized_stocks": False}) is None


def test_untagged_signal_is_caught_through_the_scanner_tags():
    state = {
        "hot_tickers": [{"symbol": "BTCUSDT", "price": 60000.0}],
        "watchlist_crypto_tickers": [
            {"symbol": "MSTRBUSDT", "instrument_type": "tokenized_stock", "underlying": "MSTR"},
        ],
        "liquid_crypto_tickers": [{"symbol": "USDCUSDT", "instrument_type": "stablecoin"}],
        "data_age_seconds": 3,
    }
    tags = instrument_tags_from_scanner_state(state, {"SPCXBUSDT": "SPCX"})
    assert tags["MSTRB-USD"] == {"instrument_type": "tokenized_stock", "underlying": "MSTR"}
    assert tags["USDC-USD"]["instrument_type"] == "stablecoin"
    assert tags["SPCXB-USD"] == {"instrument_type": "tokenized_stock", "underlying": "SPCX"}
    assert "BTC-USD" not in tags

    forward = {"ticker": "MSTRB-USD", "action": "BUY", "autonomous_source": "v2_forward"}
    assert autonomous_instrument_exclusion(forward, None, tags)["reason"] == "tokenized_stock_excluded"
    remembered = {"ticker": "SPCXB-USD", "action": "BUY"}
    assert autonomous_instrument_exclusion(remembered, None, tags)["underlying"] == "SPCX"
    assert autonomous_instrument_exclusion({"ticker": "BTC-USD", "price": 60000.0}, None, tags) is None


def test_stablecoin_pair_is_recognised_without_a_tag_but_a_token_name_is_not_guessed():
    stable = autonomous_instrument_exclusion({"ticker": "USDC-USD", "price": 1.0003})
    assert stable["reason"] == "stablecoin_excluded"
    assert stable["config_key"] == "autonomous.exclude_stablecoins"
    # ARB / BNB end in "B" like a tokenised stock; the name alone is no evidence.
    assert autonomous_instrument_exclusion({"ticker": "ARB-USD", "price": 0.9}) is None
    assert autonomous_instrument_exclusion({"ticker": "BNB-USD", "price": 600.0}) is None
    assert autonomous_instrument_exclusion({"ticker": "AAPL", "price": 200.0}) is None


def _monitor_manager(monkeypatch, candidates, config=None, scanner_state=None):
    mgr = AgentManager.__new__(AgentManager)
    scanner = SimpleNamespace(get_state=lambda: scanner_state or {}, _tokenized_stock_map={})
    mgr.agents = {"execution": type("E", (), {"is_running": True})(), "scanner": scanner}
    mgr.config_manager = SimpleNamespace(config=config or {})
    mgr._autonomous_last_decisions = {}
    monkeypatch.setattr(mgr, "_bot_mode_active", lambda: True)
    monkeypatch.setattr(mgr, "_execution_config", lambda: {
        "autonomous_candidates_enabled": True,
        "autonomous_sources": {"scanner": True, "pre_earnings": False},
        "max_autonomous_candidates_per_cycle": 4,
        "max_autonomous_trades_per_cycle": 4,
        "autonomous_cooldown_minutes": 45.0,
        "autonomous_ticker_lockout_hours": 0.0,
        "autonomous_allow_scale_in": False,
    })
    monkeypatch.setattr(mgr, "_scanner_candidates", lambda cfg: [dict(c) for c in candidates])
    sent: list = []
    monkeypatch.setattr(mgr, "on_new_signal", lambda s: sent.append(s))
    monkeypatch.setattr(AgentManager, "_autonomous_trade_allowed_now", staticmethod(lambda s: (True, "")))
    monkeypatch.setattr(mgr, "_has_open_ticker_conflict", lambda s, *, allow_scale_in: False)
    monkeypatch.setattr(mgr, "_attach_candidate_price_integrity", lambda s: s)
    events: list = []
    monkeypatch.setattr(am_mod, "record_trade_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: SimpleNamespace(get_positions=lambda: []))
    return mgr, sent, events


_TOKEN = {
    "ticker": "MSTRB-USD", "action": "BUY", "autonomous_source": "scanner",
    "instrument_type": "tokenized_stock", "underlying": "MSTR", "signal_score": 90.0,
}
_STABLE = {"ticker": "USDC-USD", "action": "BUY", "autonomous_source": "scanner", "price": 1.0, "signal_score": 85.0}
_PLAIN = {"ticker": "ETH-USD", "action": "BUY", "autonomous_source": "scanner", "price": 3000.0, "signal_score": 80.0}


def test_monitor_cycle_drops_excluded_instruments_and_names_them(monkeypatch):
    mgr, sent, events = _monitor_manager(monkeypatch, [_TOKEN, _STABLE, _PLAIN])

    result = mgr._autonomous_monitor_once()

    assert [s["ticker"] for s in sent] == ["ETH-USD"]
    reasons = {row["ticker"]: row["reason"] for row in result["excluded_instruments"]}
    assert reasons == {"MSTRB-USD": "tokenized_stock_excluded", "USDC-USD": "stablecoin_excluded"}
    assert result["skipped"]["excluded_instrument"] == 2
    assert result["sources"]["instrument_exclusions"]["excluded_count"] == 2
    traced = [e for e in events if e.get("status") == "instrument_excluded"]
    assert {e["ticker"] for e in traced} == {"MSTRB-USD", "USDC-USD"}
    assert all(e["payload"]["instrument_exclusion"]["reason"] for e in traced)

    # The trace is written once per ticker, not on every cycle.
    mgr._autonomous_last_decisions = {}
    mgr._autonomous_monitor_once()
    assert len([e for e in events if e.get("status") == "instrument_excluded"]) == 2


def test_monitor_cycle_keeps_them_when_the_owner_turns_the_exclusion_off(monkeypatch):
    config = {"autonomous": {"exclude_tokenized_stocks": False, "exclude_stablecoins": False}}
    mgr, sent, _events = _monitor_manager(monkeypatch, [_TOKEN, _PLAIN], config=config)
    result = mgr._autonomous_monitor_once()
    assert {s["ticker"] for s in sent} == {"MSTRB-USD", "ETH-USD"}
    assert result["excluded_instruments"] == []


def test_entry_handler_refuses_an_excluded_instrument_from_any_source(monkeypatch):
    """Engine and reconcile signals do not pass the candidate filter; the
    entry handler applies the same rule before any sizing or review."""
    mgr = AgentManager.__new__(AgentManager)
    state = {"watchlist_crypto_tickers": [
        {"symbol": "NVDABUSDT", "instrument_type": "tokenized_stock", "underlying": "NVDA"},
    ]}
    mgr.agents = {"scanner": SimpleNamespace(get_state=lambda: state, _tokenized_stock_map={})}
    mgr.config_manager = SimpleNamespace(agents={"execution": {}, "risk": {}}, config={})
    monkeypatch.setattr(mgr, "_bot_mode_active", lambda: True)
    events: list = []
    monkeypatch.setattr(am_mod, "record_trade_event", lambda **kwargs: events.append(kwargs))
    reached: list = []
    monkeypatch.setattr(mgr, "_apply_signal_size_caps", lambda s: reached.append(s))

    mgr.on_new_signal({"id": "s1", "ticker": "NVDAB-USD", "action": "BUY", "strategy_type": "CUSTOM", "entry": 140.0})

    assert reached == [], "an excluded instrument must stop before sizing, risk and AI review"
    assert [e["status"] for e in events] == ["instrument_excluded"]
    assert events[0]["payload"]["instrument_exclusion"]["reason"] == "tokenized_stock_excluded"
    assert events[0]["payload"]["instrument_exclusion"]["underlying"] == "NVDA"


# ---------------------------------------------------------------------------
# 3. Critic veto policy and minimum entry confidence
# ---------------------------------------------------------------------------

class _FakeProvider:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self._text, model=model or "critic-m")


def _decision(conf=0.8):
    return Decision(action=Action.OPEN_LONG, ticker="SOXLB-USD", confidence=conf,
                    thesis="fade", supporting_evidence=["s1"], opposing_evidence=["o1"])


_MODEL_VETO = ('{"challenges":["move already extended"],"categories":["catalyst_risk"],'
               '"severity":0.72,"veto":true,"rationale":"do not chase"}')


def test_model_veto_is_not_overridden_by_a_move_size_score():
    """Live: SOXLB-USD — model veto downgraded, entry accepted at AI confidence 0.35."""
    signal = {
        "ticker": "SOXLB-USD", "signal_score": 92.0, "score": 92.0,
        "signal_score_source": "move_size_heuristic", "validation_score_status": "unavailable",
    }
    verdict = run_critic(_decision(conf=0.35), signal, config={},
                         services={"critic_provider": _FakeProvider(_MODEL_VETO)})
    assert verdict.model_veto is True
    assert verdict.veto is True
    assert verdict.size_factor == 1.0
    assert verdict.policy_reason.startswith("veto:model_veto_upheld_no_validated_score")

    # A good AI confidence does not change that: the score is still unmeasured.
    confident = run_critic(_decision(conf=0.85), signal, config={},
                           services={"critic_provider": _FakeProvider(_MODEL_VETO)})
    assert confident.veto is True


def test_model_veto_override_still_applies_with_a_measured_validation_score():
    signal = {"ticker": "NVDA", "signal_score": 84.0, "validation_score": 82.0}
    verdict = run_critic(_decision(conf=0.8), signal, config={},
                         services={"critic_provider": _FakeProvider(_MODEL_VETO)})
    assert verdict.model_veto is True and verdict.veto is False
    assert verdict.policy_reason.startswith("model_veto_downgraded")
    assert "validated_score=82.0" in verdict.policy_reason
    assert 0.5 <= verdict.size_factor < 1.0


def test_a_poor_measured_score_does_not_buy_an_override():
    signal = {"ticker": "NVDA", "signal_score": 90.0, "validation_score": 40.0}
    verdict = run_critic(_decision(conf=0.8), signal, config={},
                         services={"critic_provider": _FakeProvider(_MODEL_VETO)})
    assert verdict.veto is True
    assert verdict.policy_reason.startswith("veto:model_veto_upheld_validated_score")
    assert verdict.veto_threshold == 0.65


def test_validated_score_reader_ignores_unmeasured_values():
    assert _validated_quality_score({"validation_score": 81}) == 81.0
    assert _validated_quality_score({"validation_score": 0.81}) == 81.0
    assert _validated_quality_score({"validation_score": 0}) is None
    assert _validated_quality_score({"validation_score": None, "signal_score": 95}) is None
    assert _validated_quality_score({"validation_score": 81, "validation_score_status": "unavailable"}) is None
    assert _validated_quality_score({"signal_score": 95, "score": 95, "pre_earnings_score": 88}) is None


def test_validated_score_rule_can_be_switched_off_explicitly():
    signal = {"ticker": "NVDA", "signal_score": 84.0}
    config = {"ai": {"decision": {"veto_override_requires_validated_score": False}}}
    verdict = run_critic(_decision(conf=0.8), signal, config=config,
                         services={"critic_provider": _FakeProvider(_MODEL_VETO)})
    assert verdict.model_veto is True and verdict.veto is False


def test_scanner_candidate_declares_its_score_as_a_move_size_heuristic():
    signal = build_scanner_candidate_signal(
        {"symbol": "BTCUSDT", "price": 50_000.0, "change_pct": -10.0, "volume": 50_000_000.0}, _SCANNER_CFG
    )
    assert signal["signal_score_source"] == "move_size_heuristic"
    assert "validation_score" not in signal
    assert _validated_quality_score(signal) is None


def _review(confidence, recommendation="approve", available=True):
    return {"available": available, "recommendation": recommendation, "confidence": confidence}


def test_minimum_entry_confidence_declines_a_low_confidence_approval():
    signal = {"ticker": "SOXLB-USD", "strategy_type": "SCANNER_AI_SHORT"}
    reason, detail = _ai_min_entry_confidence_gate(signal, _review(0.35), {})
    assert reason == "ai_low_entry_confidence"
    assert "0.35" in detail and "0.50" in detail and "min_entry_confidence" in detail

    assert _ai_min_entry_confidence_gate(signal, _review(0.50), {}) == ("", "")
    assert _ai_min_entry_confidence_gate(signal, _review(0.49, "reduce"), {})[0] == "ai_low_entry_confidence"
    # Configurable; 0 disables.
    strict = {"ai": {"decision": {"min_entry_confidence": 0.7}}}
    assert _ai_min_entry_confidence_gate(signal, _review(0.6), strict)[0] == "ai_low_entry_confidence"
    off = {"ai": {"decision": {"min_entry_confidence": 0}}}
    assert _ai_min_entry_confidence_gate(signal, _review(0.1), off) == ("", "")


def test_minimum_entry_confidence_leaves_other_policies_alone():
    # Pre-earnings keeps its own stricter rule.
    pe = {"ticker": "NVDA", "strategy_type": "PRE_EARNINGS_LONG"}
    assert _ai_min_entry_confidence_gate(pe, _review(0.2), {}) == ("", "")
    # An unavailable review is governed by the availability policy.
    sc = {"ticker": "ETH-USD", "strategy_type": "SCANNER_AI_LONG"}
    assert _ai_min_entry_confidence_gate(sc, _review(0.0, available=False), {}) == ("", "")
    # watch / avoid are already blocked by the AI decision itself.
    assert _ai_min_entry_confidence_gate(sc, _review(0.1, "avoid"), {}) == ("", "")


def test_entry_handler_applies_the_minimum_confidence_before_and_after_the_debate():
    source = Path(am_mod.__file__).read_text(encoding="utf-8")
    handler = source[source.index("    def on_new_signal(self, signal_data: dict):"):]
    assert handler.count("_ai_min_entry_confidence_gate(") == 2
    assert 'decision_context["ai_entry_block"]' in handler


# ---------------------------------------------------------------------------
# 4. "Risk per trade (%)" — one meaning per key
# ---------------------------------------------------------------------------

class _Capital:
    def __init__(self, balance=10_000.0):
        self._balance = balance

    def get_balance(self):
        return self._balance

    def get_positions(self):
        return []

    def get_equity_curve(self, limit=500):
        return []


def test_position_limits_read_the_legacy_key_as_notional_never_above_the_max():
    limits = resolve_position_limits(
        {"position_size_pct": 10, "max_position_size_pct": 10.0, "max_risk_per_trade_pct": 1.0}
    )
    assert limits["max_risk_per_trade_pct"] == 1.0
    assert limits["max_position_notional_pct"] == 10.0
    assert limits["base_position_notional_pct"] == 10.0
    assert limits["legacy_meaning"] == "base_position_notional_pct"

    capped = resolve_position_limits({"position_size_pct": 25, "max_position_size_pct": 4.0})
    assert capped["base_position_notional_pct"] == 4.0
    assert capped["legacy_position_size_pct"] == 25.0

    # The legacy key never feeds the risk-per-trade cap.
    no_risk_key = resolve_position_limits({"position_size_pct": 10})
    assert no_risk_key["max_risk_per_trade_pct"] == 0.5
    assert no_risk_key["max_position_notional_pct"] is None

    unset = resolve_position_limits({"max_position_size_pct": 6.0}, default_base_notional_pct=2.0)
    assert unset["base_position_notional_pct"] == 2.0 and unset["legacy_position_size_pct"] is None


def test_paper_entry_trace_legacy_10_is_a_10pct_notional_and_risk_is_capped_separately(monkeypatch):
    """What is enforced for a paper entry with the live settings
    (position_size_pct 10, max_position_size_pct 10, max_risk_per_trade_pct 1)."""
    risk_cfg = {
        "enabled": True,
        "position_size_pct": 10,
        "max_position_size_pct": 10.0,
        "max_risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 10.0,
        "max_open_trades": 5,
        "max_exposure_pct": 80.0,
        "min_risk_reward_ratio": 1.0,
    }
    config = SimpleNamespace(agents={"risk": risk_cfg}, config={})
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: _Capital())
    monkeypatch.setattr("core.capital_manager.resolve_deployable_balance", lambda cm, *a, **k: 10_000.0)

    mgr = AgentManager.__new__(AgentManager)
    mgr.config_manager = config

    # Stop 5% away: 10% notional = $1,000, loss at the stop = 0.5% of equity.
    signal = {"ticker": "AAPL", "action": "BUY", "entry": 100.0, "tp": 110.0, "sl": 95.0, "signal_score": 90.0}
    mgr._apply_signal_size_caps(signal)
    assert signal["invested_amount"] == pytest.approx(1_000.0)
    assert signal["position_size_pct"] == pytest.approx(10.0)
    # Read as "10% of equity at risk" the same number would need a $20,000
    # (200%) position on this stop. That is not what is sized.
    assert signal["invested_amount"] < 10_000.0 * 0.10 / 0.05

    result = RiskAgent(config).evaluate_signal(signal)
    assert result["accepted"] is True, result
    metrics = result["metrics"]
    assert metrics["position_size_pct"] == pytest.approx(10.0)
    assert metrics["max_position_size_pct"] == pytest.approx(10.0)
    assert metrics["risk_per_trade_pct"] == pytest.approx(0.5)
    assert metrics["max_risk_per_trade_pct"] == pytest.approx(1.0)

    # Stop 15% away: the same 10% notional would lose 1.5% > 1.0% -> rejected
    # by the risk key, not resized upward or downward.
    wide = {"ticker": "AAPL", "action": "BUY", "entry": 100.0, "tp": 130.0, "sl": 85.0, "signal_score": 90.0}
    mgr._apply_signal_size_caps(wide)
    assert wide["invested_amount"] == pytest.approx(1_000.0)
    rejected = RiskAgent(config).evaluate_signal(wide)
    assert rejected["accepted"] is False
    assert rejected["reason"] == "max_risk_per_trade_pct"
    assert rejected["metrics"]["risk_per_trade_pct"] == pytest.approx(1.5)


def test_pre_risk_cap_never_sizes_above_the_max_position_notional(monkeypatch):
    config = SimpleNamespace(
        agents={"risk": {"position_size_pct": 10, "max_position_size_pct": 4.0}}, config={}
    )
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: _Capital())
    monkeypatch.setattr("core.capital_manager.resolve_deployable_balance", lambda cm, *a, **k: 10_000.0)
    mgr = AgentManager.__new__(AgentManager)
    mgr.config_manager = config
    signal = {"ticker": "AAPL", "action": "BUY", "entry": 100.0, "tp": 110.0, "sl": 95.0}
    mgr._apply_signal_size_caps(signal)
    assert signal["invested_amount"] == pytest.approx(400.0)
    assert signal["position_size_pct"] == pytest.approx(4.0)


def test_engine_no_longer_books_the_notional_share_as_equity_at_risk():
    source = (Path(am_mod.__file__).parent / "engine.py").read_text(encoding="utf-8")
    assert "risk_per_trade = equity * (position_size_pct / 100.0)" not in source
    assert "risk_per_trade = position_notional * stop_fraction" in source
    assert 'signal_data["position_notional_pct"]' in source


# ---------------------------------------------------------------------------
# 5. Bot controller — failed persist
# ---------------------------------------------------------------------------

class _UnwritableConfig:
    """ConfigManager stand-in whose settings file cannot be written."""

    def __init__(self, enabled=False, fail=True):
        self._bot = {"enabled": enabled, "mode": "paper", "min_edge_score": 75}
        self.fail = fail
        self.save_ok = True
        self.last_save_error = None
        self.writes = []

    @property
    def bot(self):
        return self._bot

    def update_value(self, path, value):
        self.writes.append((tuple(path), value))
        self._bot[path[-1]] = value          # memory is updated even when the file is not
        if self.fail:
            self.save_ok = False
            self.last_save_error = "config.json could not be written (PermissionError)"
            return False
        self.save_ok = True
        self.last_save_error = None
        return True


@pytest.fixture
def fresh_bot_controller():
    BotController._instance = None
    yield
    BotController._instance = None


def test_failed_persist_of_start_is_taken_back(fresh_bot_controller, monkeypatch):
    config = _UnwritableConfig(enabled=False, fail=True)
    agent_manager = SimpleNamespace(calls=[])
    agent_manager.set_bot_mode = lambda enabled: agent_manager.calls.append(enabled)
    controller = BotController(config_manager=config, agent_manager=agent_manager)
    monkeypatch.setattr(controller, "_snapshot_day_start", lambda: None)
    started_threads = []
    monkeypatch.setattr(controller, "_start_background_threads", lambda: started_threads.append(1))

    result = controller.start()

    assert result["status"] == "error"
    assert result["code"] == "config_not_saved" and result["persisted"] is False
    assert "NOT saved" in result["message"] and "not started" in result["message"]
    assert controller._running is False
    assert controller.is_running is False, "the in-memory switch must be reverted too"
    assert config.bot["enabled"] is False
    assert agent_manager.calls == [], "the engine must not be told the bot is on"
    assert started_threads == []


def test_successful_start_is_unchanged(fresh_bot_controller, monkeypatch):
    config = _UnwritableConfig(enabled=False, fail=False)
    controller = BotController(config_manager=config, agent_manager=None)
    monkeypatch.setattr(controller, "_snapshot_day_start", lambda: None)
    monkeypatch.setattr(controller, "_start_background_threads", lambda: None)
    assert controller.start()["status"] == "started"
    assert controller.is_running is True and config.bot["enabled"] is True


def test_failed_persist_of_stop_stays_stopped_and_says_it_is_not_durable(fresh_bot_controller, monkeypatch):
    config = _UnwritableConfig(enabled=False, fail=False)
    agent_manager = SimpleNamespace(calls=[])
    agent_manager.set_bot_mode = lambda enabled: agent_manager.calls.append(enabled)
    controller = BotController(config_manager=config, agent_manager=agent_manager)
    monkeypatch.setattr(controller, "_snapshot_day_start", lambda: None)
    monkeypatch.setattr(controller, "_start_background_threads", lambda: None)
    assert controller.start()["status"] == "started"

    config.fail = True
    result = controller.stop(reason="api_request")

    assert result["status"] == "stopped"
    assert result["durable"] is False and result["persisted"] is False
    assert "not durable" in result["warning"] and "NOT saved" in result["warning"]
    assert controller._running is False and controller.is_running is False
    assert config.bot["enabled"] is False
    assert agent_manager.calls[-1] is False
    assert controller._stop_not_durable is True

    # Once the file is writable again the next status poll makes the stop durable.
    config.fail = False
    status = controller.get_control_status()
    assert status["running"] is False and status["stop_not_durable"] is False
    assert config.writes[-1] == (("bot", "enabled"), False)


def test_durable_stop_reports_durable(fresh_bot_controller, monkeypatch):
    config = _UnwritableConfig(enabled=True, fail=False)
    controller = BotController(config_manager=config, agent_manager=None)
    result = controller.stop(reason="manual")
    assert result["status"] == "stopped" and result["durable"] is True
    assert "warning" not in result


# ---------------------------------------------------------------------------
# 6. Watchlist quick AI
# ---------------------------------------------------------------------------

import core.watchlist_quick_ai as quick_ai  # noqa: E402


class _Ollama:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def check_availability(self):
        return True

    def get_best_model(self):
        return "test-model"

    def generate(self, prompt, model=None, system="", **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.reply


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(quick_ai, "_CACHE", {})
    monkeypatch.setattr("core.ollama_client._get_ollama_config", lambda: {})

    def _install(reply):
        fake = _Ollama(reply)
        monkeypatch.setattr(quick_ai, "ollama", fake)
        return fake

    return _install


def test_quick_ai_passes_a_bounded_timeout(quick, monkeypatch):
    fake = quick({"response": '{"bias":"bullish","confidence":70,"action":"BUY"}'})
    out = quick_ai.run_quick_ai_for_ticker("AAPL", {"price": 200.0}, {"enabled": True})
    assert out["available"] is True
    assert fake.calls[0]["timeout_override"] == 45
    assert quick_ai.DEFAULT_QUICK_AI_TIMEOUT_SECONDS == 45

    monkeypatch.setattr("core.ollama_client._get_ollama_config", lambda: {"quick_ai_timeout_seconds": 20})
    assert quick_ai.quick_ai_timeout_seconds() == 20
    monkeypatch.setattr("core.ollama_client._get_ollama_config", lambda: {"quick_ai_timeout_seconds": 9999})
    assert quick_ai.quick_ai_timeout_seconds() == 120
    monkeypatch.setattr("core.ollama_client._get_ollama_config", lambda: {"quick_ai_timeout_seconds": "x"})
    assert quick_ai.quick_ai_timeout_seconds() == 45


def test_quick_ai_timeout_is_an_explicit_unavailable_state(quick):
    quick({"error": "HTTPConnectionPool(host='localhost', port=11434): Read timed out. (read timeout=45)"})
    out = quick_ai.run_quick_ai_for_ticker("AAPL", {"price": 200.0}, {"enabled": True})
    assert out["available"] is False
    assert out["unavailable_reason"] == "timeout"
    assert out["timeout_seconds"] == 45
    assert "did not answer within 45 s" in out["error"]
    # The neutral placeholder is not an analysis.
    assert out["analysis"]["action"] == "WAIT" and out["analysis"]["confidence"] == 0


def test_quick_ai_other_call_failures_are_not_reported_as_an_empty_answer(quick):
    quick({"error": "Ollama error: 500"})
    out = quick_ai.run_quick_ai_for_ticker("AAPL", {"price": 200.0}, {"enabled": True})
    assert out["available"] is False and out["unavailable_reason"] == "call_failed"
    assert "Ollama error: 500" in out["error"]


def test_quick_ai_prompt_states_what_the_price_is():
    prompt = quick_ai._prompt_from_context(
        "AAPL", {"price": 201.5, "change_pct": 1.2, "price_as_of": "2026-11-13", "session": "closed"}
    )
    assert "Price: 201.5\n" in prompt
    assert "Price as of: 2026-11-13\n" in prompt
    assert "Session: closed\n" in prompt

    withheld = quick_ai._prompt_from_context(
        "AAPL", {"price_omitted_reason": "last engine update is 9 day(s) old - too old to use as the current price"}
    )
    assert "Price: unknown (last engine update is 9 day(s) old" in withheld
    assert "Price as of: unknown\n" in withheld and "Session: unknown\n" in withheld


# ---------------------------------------------------------------------------
# 7. Wording and the direction-score input share cap
# ---------------------------------------------------------------------------

def test_probability_gate_text_says_setup_score_not_probability():
    source = (Path(am_mod.__file__).parent / "scoring_system.py").read_text(encoding="utf-8")
    assert "Rejected: prob " not in source
    assert "(not a probability)" in source


_NOMINAL = {"c1": 0.25, "c2": 0.30, "c3": 0.25, "c4": 0.20, "c5": 0.12, "c6": 0.10}


def test_share_cap_constant_and_no_effect_when_inputs_are_present():
    assert DIRECTION_INPUT_MAX_SHARE_MULTIPLE == 2.0
    full = {name: (70.0, weight) for name, weight in _NOMINAL.items()}
    result = cap_direction_input_shares(full)
    assert result["cap_applied"] is False
    assert result["score"] == pytest.approx(70.0)
    assert result["evidence_factor"] == pytest.approx(1.0)
    # More than half of the nominal weight present: still the plain average.
    most = dict(full, c5=(None, 0.12), c6=(None, 0.10))
    assert cap_direction_input_shares(most)["cap_applied"] is False


def test_share_cap_limits_a_single_input_to_twice_its_nominal_share():
    # Only neutral news (weight 0.20) and retail positioning (0.10) are present.
    components = {name: (None, weight) for name, weight in _NOMINAL.items()}
    components["c4"] = (50.0, 0.20)
    components["c6"] = (83.0, 0.10)
    result = cap_direction_input_shares(components)

    nominal_total = sum(_NOMINAL.values())
    assert result["uncapped_score"] == pytest.approx(61.0)          # retail was a third of the score
    assert result["cap_applied"] is True
    assert result["shares"]["c6"] == pytest.approx(2.0 * 0.10 / nominal_total, abs=1e-4)
    assert result["shares"]["c6"] <= 2.0 * 0.10 / nominal_total + 1e-4
    assert result["shares"]["c4"] == pytest.approx(2.0 * 0.20 / nominal_total, abs=1e-4)
    assert result["neutral_share"] == pytest.approx(1.0 - 2.0 * 0.30 / nominal_total, abs=1e-4)
    assert 50.0 < result["score"] < result["uncapped_score"]
    assert result["evidence_factor"] == pytest.approx(2.0 * 0.30 / nominal_total, abs=1e-4)


def test_direction_bias_with_only_retail_positioning_is_pulled_toward_neutral():
    """Live: GRRR +33 with zero news — eToro positioning was a third of the score."""
    from agents.pre_earnings_agent import compute_direction_bias_score

    retail = {"buy_pct": 99.0, "sell_pct": 1.0, "popularity_rank": 300}
    result = compute_direction_bias_score("GRRR", {}, 0.0, retail=retail, retail_weight=0.10)

    assert result["uncapped_bias_score"] == pytest.approx(32.67, abs=0.05)
    assert result["input_share_cap_applied"] is True
    assert result["input_share_cap_multiple"] == 2.0
    assert result["input_shares"]["c6_retail"] <= 2.0 * 0.10 / 1.22 + 1e-4
    assert 0 < result["bias_score"] < result["uncapped_bias_score"] / 2 + 1
    assert result["evidence_factor"] < 0.5
    # Same crowd skew with the main inputs present: no cap, score unchanged
    # from the plain weighted average.
    enriched = {
        "beat_rate_historical": 0.5, "beat_rate_samples": 8,
        "analyst_target_upside_pct": 0.0, "analyst_rec_mean": 3.0,
        "calls_vs_puts_ratio": 1.0,
    }
    supported = compute_direction_bias_score("GRRR", enriched, 0.0, retail=retail, retail_weight=0.10)
    assert supported["input_share_cap_applied"] is False
    assert supported["bias_score"] == supported["uncapped_bias_score"]


def test_pre_earnings_agent_lowers_the_direction_band_score_when_the_cap_applied():
    source = (Path(am_mod.__file__).parent.parent / "agents" / "pre_earnings_agent.py").read_text(encoding="utf-8")
    assert 'db_dict.get("input_share_cap_applied")' in source
    assert '_band["input_coverage_pct"]' in source


# ---------------------------------------------------------------------------
# 8. Engine process: cached news only
# ---------------------------------------------------------------------------

def _scanner_signal():
    return {
        "ticker": "OKTA", "autonomous_source": "scanner", "signal_mode": "AUTONOMOUS_SCANNER",
        "news_sentiment": "unknown", "news_headlines": [],
    }


def _capture_hydration(monkeypatch):
    seen = {}

    def fake_hydrate(ticker, ttl_seconds=45, max_items=180, allow_remote=True, force_refresh=False):
        seen.update(allow_remote=allow_remote, force_refresh=force_refresh)
        return {"top_headlines": [], "hydration": {"requested": True, "status": "miss_cache_only"}}

    monkeypatch.setattr("core.news_pulse.hydrate_news_pulse", fake_hydrate)
    return seen


_NEWS_CFG = {"autonomous_news_gate": {
    "allow_blocking_fetch": True, "scanner_auto_fetch": True, "scanner_force_refresh_on_signal": True,
}}


def test_engine_process_never_asks_for_a_remote_news_refresh(monkeypatch):
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    seen = _capture_hydration(monkeypatch)
    result = _attach_cached_news_context_for_ai(_scanner_signal(), _NEWS_CFG)
    assert seen == {"allow_remote": False, "force_refresh": False}
    receipt = result["news_context"]["hydration"]
    assert receipt["cached_only"] is True
    assert receipt["cached_only_reason"] == "engine_process_reads_cached_news_only"


def test_api_process_keeps_the_refresh(monkeypatch):
    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    seen = _capture_hydration(monkeypatch)
    result = _attach_cached_news_context_for_ai(_scanner_signal(), _NEWS_CFG)
    assert seen == {"allow_remote": True, "force_refresh": True}
    assert "cached_only" not in result["news_context"]["hydration"]


# ---------------------------------------------------------------------------
# 1b. Trade Radar — rule applied to what is served, and to exchange pairs
# ---------------------------------------------------------------------------

def test_served_candidate_analysed_before_the_rule_is_blocked_on_read():
    """Live payload: SHELLUSDT READY, confidence 62, backtest 37 trades PF 0.587."""
    item = {
        "symbol": "SHELLUSDT",
        "analysis": {"verdict": "LONG", "confidence": 62, "entry": 10.0, "tp": 11.0, "sl": 9.5},
        "backtest": {"available": True, "closed_trades": 37, "profit_factor": 0.587, "win_rate": 26.0},
        "readiness": {"status": "READY", "can_open": True, "blockers": []},
        "red_flags": ["no_symbol_news"],
    }
    assert TradeRadarAgent._apply_backtest_verdict_on_read(item, {}) is True
    assert item["backtest"]["negative"] is True
    assert item["readiness"]["status"] == "BLOCKED" and item["readiness"]["can_open"] is False
    assert item["readiness"]["blockers"] == ["backtest_negative"]
    assert item["red_flags"][0] == "backtest_negative"
    # Idempotent: a second read changes nothing.
    assert TradeRadarAgent._apply_backtest_verdict_on_read(item, {}) is False

    healthy = {
        "symbol": "KAVAUSDT",
        "analysis": {"verdict": "LONG", "confidence": 80, "entry": 10.0, "tp": 11.0, "sl": 9.5},
        "backtest": {"available": True, "closed_trades": 25, "profit_factor": 2.7},
        "readiness": {"status": "READY", "can_open": True, "blockers": []},
        "red_flags": [],
    }
    assert TradeRadarAgent._apply_backtest_verdict_on_read(healthy, {}) is False
    assert healthy["readiness"]["status"] == "READY" and healthy["backtest"]["negative"] is False


def test_radar_block_reaches_a_crypto_signal_despite_the_pair_naming():
    radar = [
        {"symbol": "SHELLUSDT", "analysis": {"confidence": 62},
         "readiness": {"status": "BLOCKED", "can_open": False, "blockers": ["backtest_negative"]}},
        {"symbol": "KAVAUSDT", "analysis": {"confidence": 80},
         "readiness": {"status": "READY", "can_open": True, "blockers": []}},
    ]
    assert _trade_radar_opportunity(radar, "SHELL-USD") == (0.0, "trade_radar_blocked=backtest_negative")
    # An unblocked pair gains nothing from the looser match (unchanged result).
    assert _trade_radar_opportunity(radar, "KAVA-USD") == (0.0, "ticker_not_in_trade_radar")
    assert _trade_radar_opportunity(radar, "KAVAUSDT") == (80.0, "trade_radar_opportunity=80.0")
