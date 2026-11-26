"""tests/integration/test_execution_ticker_learning.py
=====================================================
Integration tests for the ticker-learning wiring in:
  * agents/execution_agent.py :: _process_paper_signal
  * core/agent_manager.py    :: _autonomous_monitor_once (candidate stamp)

These tests drive the real production code paths end-to-end (no mocks of the
units under test) and only stub the collaborators (capital manager, signal
manager, trade-audit sink). They verify that the per-ticker learning layer
gates re-entry (BLOCK cooldown / raised bar / REDUCE size) and presses the
edge on win streaks (wider R:R), and that the autonomous monitor stamps the
adjustment on the candidate before it is sent to execution.

Patch-target note (Python import-binding semantics): execution_agent.py
does ``from core.capital_manager import get_capital_manager`` etc. at module
top, so inside that module ``get_capital_manager`` is a *module-level binding*.
To intercept calls inside _process_paper_signal we patch
``agents.execution_agent.get_capital_manager`` (NOT ``core.capital_manager.*``).
"""
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.execution_agent import ExecutionAgent  # noqa: E402
from core.agent_manager import AgentManager  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fakes
# ─────────────────────────────────────────────────────────────────────────────

def _loss_pos(ticker: str, direction: str, closed_at: datetime, pnl: float = -1.0) -> dict:
    return {
        "ticker": ticker,
        "status": "CLOSED",
        "direction": direction,
        "pnl": pnl,
        "closed_at": closed_at.isoformat(),
    }


def _win_pos(ticker: str, direction: str, closed_at: datetime, pnl: float = 1.0) -> dict:
    return {
        "ticker": ticker,
        "status": "CLOSED",
        "direction": direction,
        "pnl": pnl,
        "closed_at": closed_at.isoformat(),
    }


def _make_signal(score: float = 80.0, action: str = "BUY") -> dict:
    """Fresh signal payload for _process_paper_signal. Pre-set decision_context
    so build_signal_decision_context is not invoked (keeps the test path
    focused on the ticker-learning wiring)."""
    return {
        "ticker": "ETH-USD",
        "action": action,
        "id": "sig-1",
        "entry": 100.0,
        "tp": 105.0,
        "sl": 98.0,
        "signal_score": score,
        "strategy_type": "scanner",
        "decision_context": {},
    }


def _base_cfg(**overrides) -> dict:
    cfg = {
        "max_open_trades": 10,
        "balance_usage_pct": 10.0,
        "unprofitable_balance_usage_cap_pct": 0.5,
        "profitability_min_sample": 30,
        "profitability_min_profit_factor": 1.2,
        "profitability_min_expectancy": 0.0,
        # These tests isolate ticker-learning behavior. Production enables the
        # autonomous profitability gate; disable it here so the unrelated gate
        # does not mask the behavior under test.
        "autonomous_profitability_gate": {"enabled": False},
        "capital_entry_gate": {"enabled": False},
        "force_one_x_until_profitable": True,
        "allow_ai_leverage": True,
        "max_ai_leverage": 2,
        "scanner_min_score": 78.0,
        "ticker_learning": {
            "enabled": True,
            "window": 20,
            "loss_streak_reduce": 3,
            "loss_streak_tighten": 4,
            "loss_streak_block": 5,
            "block_cooldown_hours": 6,
            "win_streak_boost": 3,
            "win_streak_boost2": 5,
            "max_leverage_cap": 5,
            "size_mult_l3": 0.5,
            "size_mult_l4": 0.25,
            "min_score_l3": 90.0,
            "min_score_l4": 93.0,
            "rr_mult_w3": 1.25,
            "rr_mult_w5": 1.5,
            "baseline_min_score": 78.0,
        },
    }
    cfg.update(overrides)
    return cfg


class _FakeCM:
    """Capital-manager stand-in. Records open_position kwargs."""

    def __init__(self, positions: list[dict], balance: float = 10000.0):
        self._positions = positions
        self._balance = balance
        self.open_calls: list[dict] = []
        self.attach_chart_calls: list[tuple[str, dict]] = []

    def get_positions(self) -> list[dict]:
        return list(self._positions)

    def get_balance(self) -> float:
        return self._balance

    def get_position(self, position_id: str):
        return next(
            (dict(item) for item in self._positions if item.get("position_id") == position_id),
            None,
        )

    def open_position(self, **kwargs):
        self.open_calls.append(dict(kwargs))
        position = {
            "position_id": "pos-test",
            "entry_price": float(kwargs.get("size_usd", 0.0)),
            "stop_loss_price": 98.0,
            "take_profit_price": 105.0,
            "usd_size": float(kwargs.get("size_usd", 0.0)),
            "direction": kwargs.get("direction"),
            "ticker": kwargs.get("ticker"),
            "signal_id": kwargs.get("signal_id"),
            "status": "OPEN",
            "decision_context": kwargs.get("decision_context") or {},
        }
        self._positions.append(position)
        return dict(position)

    def attach_entry_chart_snapshot(self, position_id, snapshot):
        self.attach_chart_calls.append((position_id, dict(snapshot)))
        return {**self.open_calls[-1], "position_id": position_id, "entry_chart_snapshot": dict(snapshot)}


class _FakeSM:
    """Signal-manager stand-in: no existing paper position for any id."""

    def __init__(self):
        self.open_signals: dict = {}
        self.attached: list[tuple] = []

    def attach_paper_position(self, signal_id, position):
        self.attached.append((signal_id, position))


class _StubConfigManager:
    """Minimal config_manager for BaseAgent.__init__ — agents dict unused
    because we call _process_paper_signal directly with our own cfg."""

    def __init__(self):
        self.agents = {
            "execution": {"enabled": True, "mode": "paper"},
            "risk": {},
        }
        self.bot = {"enabled": True}

    @property
    def config(self):
        agents = {
            "execution": {"enabled": True, "mode": "paper"},
            **self.agents,
        }
        return {
            "autonomy": {"mode": "PAPER_AUTO", "kill_switch_active": False},
            "paper_execution": {"reduce_fraction_pct": 25.0},
            "agents": agents,
            "risk_constitution": {
                "owner_confirmed": True,
                "max_risk_per_trade_pct": 0.6,
                "max_position_size_pct": 2.0,
                "max_total_exposure_pct": 20.0,
                "max_leverage": 5,
                "minimum_cash_pct": 30.0,
                "max_positions": 10,
                "max_sector_exposure_pct": 10.0,
                "correlation_threshold": 0.75,
                "max_correlated_exposure_pct": 15.0,
                "max_liquidity_participation_pct": 0.5,
                "max_daily_loss_pct": 2.0,
                "max_daily_loss_trades": 2,
                "max_drawdown_pct": 8.0,
                "max_data_age_seconds": 120,
                "block_data_conflicts": True,
                "require_approved_strategy": False,
            },
        }


@pytest.fixture
def exec_setup(monkeypatch, tmp_path):
    """Wire fakes into the execution_agent module-level bindings and yield a
    namespace with holder controls. Each test sets holder['cm'] to the desired
    _FakeCM."""
    holder = {"cm": _FakeCM([])}
    monkeypatch.setenv("MS_EXECUTION_DB_PATH", str(tmp_path / "execution.db"))
    from core.execution import runtime as execution_runtime

    execution_runtime._BROKERS.clear()

    # Sizing must be deterministic and independent of the operator's live
    # paper journal in data/trade_journal.json.
    monkeypatch.setattr("core.feedback_loop.get_recent_trades", lambda *_args, **_kwargs: [])

    def _factory():
        return holder["cm"]

    monkeypatch.setattr(
        "agents.execution_agent.get_capital_manager",
        _factory,
        raising=True,
    )

    sm = _FakeSM()
    monkeypatch.setattr(
        "agents.execution_agent.get_signal_manager",
        lambda: sm,
        raising=True,
    )

    events: list[dict] = []

    def _capture_event(**kwargs):
        events.append(dict(kwargs))

    monkeypatch.setattr(
        "agents.execution_agent.record_trade_event",
        _capture_event,
        raising=True,
    )

    agent = ExecutionAgent(_StubConfigManager())
    return SimpleNamespace(holder=holder, sm=sm, events=events, agent=agent)


def _run(agent, cfg, signal_score=80.0, signal_id="sig-1"):
    """Run _process_paper_signal with a fresh signal, return (ok, cm)."""
    sig = _make_signal(score=signal_score)
    sig["id"] = signal_id
    ok = agent._process_paper_signal(cfg, "ETH-USD", "BUY", signal_id, sig, risk_evaluation=None)
    return ok, sig


def test_s8_execution_captures_30m_entry_chart_after_paper_fill(exec_setup, monkeypatch):
    snapshot = {
        "available": True,
        "capture_status": "captured",
        "snapshot_id": "entry_" + "b" * 24,
        "url": "/api/entry-chart/entry_" + "b" * 24,
        "timeframe": "30m",
    }
    captured = []

    def _capture(**kwargs):
        captured.append(kwargs)
        return snapshot

    monkeypatch.setattr("agents.execution_agent.capture_entry_chart_snapshot", _capture)
    signal = _make_signal(score=90.0)
    signal["strategy_type"] = "S8_SUPERTREND_BOS"
    signal["decision_context"] = {
        "strategy_type": "S8_SUPERTREND_BOS",
        "strategy_evidence": {"primary_timeframe": "30m"},
    }

    ok = exec_setup.agent._process_paper_signal(
        _base_cfg(), "ETH-USD", "BUY", "sig-1", signal, risk_evaluation=None
    )

    assert ok is True
    assert captured[0]["timeframe"] == "30m"
    assert captured[0]["position"]["position_id"] == "pos-test"
    assert exec_setup.holder["cm"].attach_chart_calls == [("pos-test", snapshot)]
    assert exec_setup.sm.attached[0][1]["entry_chart_snapshot"] == snapshot


def test_execution_agent_honors_persisted_master_switch(exec_setup):
    """Direct execution calls cannot bypass the persisted Agents master switch."""
    exec_setup.agent.config_manager.bot = {"enabled": False}
    exec_setup.agent.config_manager.agents = {"execution": {"enabled": True, "mode": "paper"}}

    assert exec_setup.agent.process_signal(_make_signal()) is False
    assert any(event.get("status") == "bot_stopped" for event in exec_setup.events)


@pytest.mark.parametrize(
    ("risk_limit", "expected_open"),
    [(0.4, False), (0.6, True)],
)
def test_execution_final_risk_gate_uses_leveraged_notional(
    exec_setup,
    risk_limit,
    expected_open,
):
    exec_setup.agent.config_manager.agents = {
        "risk": {"max_risk_per_trade_pct": risk_limit}
    }
    signal = _make_signal(score=90.0)
    signal.update({"sl": 80.0, "tp": 140.0, "leverage": 5})
    cfg = _base_cfg(
        force_one_x_until_profitable=False,
        max_ai_leverage=5,
        unprofitable_balance_usage_cap_pct=0.5,
        unprofitable_balance_usage_hard_cap_pct=0.5,
    )

    opened = exec_setup.agent._process_paper_signal(
        cfg,
        "ETH-USD",
        "BUY",
        "sig-1",
        signal,
        risk_evaluation={"accepted": True, "metrics": {}},
    )

    assert opened is expected_open
    assert signal["final_risk_per_trade_pct"] == 0.5
    if expected_open:
        assert len(exec_setup.holder["cm"].open_calls) == 1
    else:
        assert exec_setup.holder["cm"].open_calls == []
        assert any(
            event.get("status") == "max_risk_per_trade_pct"
            for event in exec_setup.events
        )


def test_execution_applies_confidence_factor_to_pre_sized_autonomous_signal(
    exec_setup,
):
    """Pre-risk sizing must not bypass the later confidence reduction."""
    exec_setup.agent.config_manager.agents = {
        "risk": {"max_risk_per_trade_pct": 5.0}
    }
    signal = _make_signal(score=90.0)
    signal.update(
        {
            "invested_amount": 1_000.0,
            "usd_size": 1_000.0,
            "adjusted_size_factor": 0.25,
            "_autonomous_candidate": True,
        }
    )

    opened = exec_setup.agent._process_paper_signal(
        _base_cfg(
            balance_usage_pct=10.0,
            unprofitable_balance_usage_cap_pct=10.0,
            unprofitable_balance_usage_hard_cap_pct=10.0,
        ),
        "ETH-USD",
        "BUY",
        "sig-1",
        signal,
        risk_evaluation={"accepted": True, "metrics": {}},
    )

    assert opened is True
    assert exec_setup.holder["cm"].open_calls[0]["size_usd"] == pytest.approx(250.0)
    assert signal["invested_amount"] == pytest.approx(250.0)
    assert signal["decision_context"]["sizing"]["size_factor"] == pytest.approx(0.25)


def test_execution_rechecks_capital_profitability_gate(exec_setup, monkeypatch):
    """A direct executor call cannot bypass Capital's evidence bundle."""
    cfg = _base_cfg(
        autonomous_profitability_gate={
            "enabled": True,
            "min_sample": 3,
            "min_profit_factor": 1.2,
            "min_expectancy_usd": 0.0,
            "max_drawdown_pct": 25.0,
        }
    )
    positions = [
        {
            "status": "CLOSED",
            "strategy_type": "SCANNER_AI_LONG",
            "direction": "LONG",
            "net_pnl": -10.0,
            "pnl_pct_notional": -1.0,
            "closed_at": f"2026-07-0{i}T10:00:00+00:00",
            "decision_context": {"entry_strategy": "fade"},
        }
        for i in range(1, 4)
    ]
    exec_setup.holder["cm"] = _FakeCM(positions)
    monkeypatch.setattr(
        "agents.execution_agent.evaluate_capital_entry",
        lambda *_args, **_kwargs: {
            "allowed": False,
            "check": "realized_performance",
            "status": "negative_edge",
            "reason": "Matching realised Capital edge is negative",
            "checks": {},
        },
    )
    signal = _make_signal()
    signal.update({"strategy_type": "SCANNER_AI_LONG", "entry_strategy": "fade"})

    ok = exec_setup.agent._process_paper_signal(
        cfg, "ETH-USD", "BUY", "sig-1", signal, risk_evaluation=None
    )

    assert ok is False
    assert exec_setup.holder["cm"].open_calls == []
    assert any(event.get("status") == "profitability_gate" for event in exec_setup.events)


def test_execution_rechecks_source_backed_ai_news_gate(exec_setup, monkeypatch):
    """A direct executor call must fail closed when fresh AI News evidence is absent."""
    signal = _make_signal()
    signal.update({"strategy_type": "SCANNER_AI_LONG", "entry_strategy": "fade"})
    monkeypatch.setattr(
        "agents.execution_agent.evaluate_capital_entry",
        lambda *_args, **_kwargs: {
            "allowed": False,
            "check": "news",
            "status": "ai_analysis_unavailable",
            "reason": "Need 1 AI-analyzed article(s) in the last 24h; found 0",
            "checks": {"news": {"allowed": False, "status": "ai_analysis_unavailable", "context": {}}},
        },
    )

    ok = exec_setup.agent._process_paper_signal(
        _base_cfg(), "ETH-USD", "BUY", "sig-1", signal, risk_evaluation=None
    )

    assert ok is False
    assert exec_setup.holder["cm"].open_calls == []
    assert any(event.get("status") == "news_gate" for event in exec_setup.events)


def test_execution_opens_s7_paper_experiment_as_standard_capital_position(exec_setup, monkeypatch):
    """The explicit paper S7 gate must not route the entry into research_cohort."""
    signal = _make_signal(score=90.0)
    signal.update(
        {
            "strategy_type": "S7_LIQUIDITY_SWEEP_REVERSAL",
            "strategy_source": "V2_FORWARD_RESEARCH",
            "autonomous_source": "v2_forward",
            "research_only": True,
            "capital_execution_eligible": False,
        }
    )
    monkeypatch.setattr(
        "agents.execution_agent.evaluate_capital_entry",
        lambda *_args, **_kwargs: {
            "allowed": True,
            "check": "paper_s7_experiment",
            "status": "paper_only",
            "reason": "S7 paper Capital experiment passed data integrity",
            "paper_capital_experiment": True,
            "checks": {"data": {"allowed": True}},
        },
    )

    ok = exec_setup.agent._process_paper_signal(
        _base_cfg(capital_entry_gate={"enabled": False}),
        "ETH-USD",
        "BUY",
        "sig-1",
        signal,
        risk_evaluation=None,
    )

    assert ok is True
    assert signal["research_only"] is False
    assert signal["capital_execution_eligible"] is True
    call = exec_setup.holder["cm"].open_calls[0]
    assert "position_type" not in call
    assert "research_only" not in call
    assert call["decision_context"]["paper_capital_experiment"] is True
    assert call["decision_context"]["execution_context"]["paper_capital_experiment"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Part A — _process_paper_signal ticker-learning wiring
# ─────────────────────────────────────────────────────────────────────────────

def test_ticker_cooldown_skips(exec_setup, monkeypatch):
    """5 CLOSED LOSS positions on ETH-USD/LONG, most recent 1h ago -> BLOCK
    cooldown: _process_paper_signal returns False, ticker_cooldown event is
    recorded, and open_position is never called."""
    now = datetime.now(timezone.utc)
    positions = [
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=1)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=3)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=5)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=7)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=9)),
    ]
    exec_setup.holder["cm"] = _FakeCM(positions)

    cfg = _base_cfg()
    ok, _sig = _run(exec_setup.agent, cfg, signal_score=80.0)

    assert ok is False, "5-loss cooldown must skip execution"
    assert any(e.get("status") == "ticker_cooldown" for e in exec_setup.events), (
        f"expected a ticker_cooldown event; got {exec_setup.events}"
    )
    assert len(exec_setup.holder["cm"].open_calls) == 0, (
        "open_position must NOT be called during cooldown"
    )


def test_ticker_bar_raised_skips(exec_setup, monkeypatch):
    """3 CLOSED LOSS positions -> streak 3 REDUCE, min_score_required 90.
    signal_score 80 < 90 -> bar raised, skip, no open_position call."""
    now = datetime.now(timezone.utc)
    positions = [
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=2)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=4)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=6)),
    ]
    exec_setup.holder["cm"] = _FakeCM(positions)

    cfg = _base_cfg()
    ok, _sig = _run(exec_setup.agent, cfg, signal_score=80.0)

    assert ok is False, "below-raised-bar must skip execution"
    assert any(e.get("status") == "ticker_bar_raised" for e in exec_setup.events), (
        f"expected a ticker_bar_raised event; got {exec_setup.events}"
    )
    assert len(exec_setup.holder["cm"].open_calls) == 0, (
        "open_position must NOT be called when bar is raised"
    )


def test_losing_ticker_reduces_size(exec_setup, monkeypatch):
    """3 CLOSED LOSS positions, signal_score 95 (>= 90 bar) -> passes, but size
    is reduced (size_mult 0.5). The size_usd passed to open_position must be
    SMALLER than the no-adjustment baseline, and decision_context.ticker_learning
    action == REDUCE."""
    # Patch _resolve_balance_usage_pct to a deterministic value so the only
    # variable between reduced and baseline is the ticker adjustment.
    monkeypatch.setattr(
        ExecutionAgent,
        "_resolve_balance_usage_pct",
        staticmethod(lambda cfg, **_kwargs: (0.05, {"profitable": False})),
    )

    now = datetime.now(timezone.utc)
    positions = [
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=2)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=4)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=6)),
    ]
    exec_setup.holder["cm"] = _FakeCM(positions)
    cfg = _base_cfg()
    ok, _sig = _run(exec_setup.agent, cfg, signal_score=95.0)
    assert ok is True, "score 95 >= bar 90 must pass"
    assert len(exec_setup.holder["cm"].open_calls) == 1, "open_position must be called once"
    reduced_call = exec_setup.holder["cm"].open_calls[0]
    reduced_size = float(reduced_call["size_usd"])
    assert reduced_call["decision_context"]["ticker_learning"]["action"] == "REDUCE", (
        "decision_context must carry ticker_learning action REDUCE"
    )

    # Baseline: same signal with NO closed positions (no adjustment).
    from core.execution import runtime as execution_runtime
    execution_runtime._BROKERS.clear()
    monkeypatch.setenv("MS_EXECUTION_DB_PATH", os.environ["MS_EXECUTION_DB_PATH"] + ".baseline")
    exec_setup.holder["cm"] = _FakeCM([])
    ok_base, _sig_base = _run(exec_setup.agent, cfg, signal_score=95.0, signal_id="sig-baseline")
    assert ok_base is True
    assert len(exec_setup.holder["cm"].open_calls) == 1
    baseline_size = float(exec_setup.holder["cm"].open_calls[0]["size_usd"])

    assert reduced_size < baseline_size, (
        f"reduced size {reduced_size} must be < baseline {baseline_size}"
    )


def test_winning_ticker_not_profitable_keeps_leverage_widens_tp(exec_setup, monkeypatch):
    """3 CLOSED WIN positions -> rr_mult 1.25, leverage_bonus 1. But
    profitability_profile not profitable -> force_one_x keeps leverage 1 and
    the leverage bonus is skipped. take_profit_pct must be 1.25x baseline
    (0.05 -> 0.0625)."""
    monkeypatch.setattr(
        ExecutionAgent,
        "_resolve_balance_usage_pct",
        staticmethod(lambda cfg, **_kwargs: (0.05, {"profitable": False})),
    )

    now = datetime.now(timezone.utc)
    positions = [
        _win_pos("ETH-USD", "LONG", now - timedelta(hours=2)),
        _win_pos("ETH-USD", "LONG", now - timedelta(hours=4)),
        _win_pos("ETH-USD", "LONG", now - timedelta(hours=6)),
    ]
    exec_setup.holder["cm"] = _FakeCM(positions)
    cfg = _base_cfg()  # allow_ai_leverage True, force_one_x_until_profitable True
    ok, _sig = _run(exec_setup.agent, cfg, signal_score=80.0)
    assert ok is True
    assert len(exec_setup.holder["cm"].open_calls) == 1
    call = exec_setup.holder["cm"].open_calls[0]
    # Baseline TP pct = |105-100|/100 = 0.05; widened by rr_mult 1.25 -> 0.0625.
    assert call["take_profit_pct"] == pytest.approx(0.05 * 1.25, rel=1e-9), (
        f"win-streak TP must be widened to 0.0625; got {call['take_profit_pct']}"
    )
    # Not profitable + force_one_x -> leverage 1 (bonus skipped).
    assert call["leverage"] == 1, (
        f"leverage must stay 1 when not profitable; got {call['leverage']}"
    )


def test_enabled_false_no_adjustment(exec_setup, monkeypatch):
    """ticker_learning.enabled=False with 5 LOSS positions: behavior == baseline
    (no cooldown skip, no bar raise, normal size, normal TP)."""
    monkeypatch.setattr(
        ExecutionAgent,
        "_resolve_balance_usage_pct",
        staticmethod(lambda cfg, **_kwargs: (0.05, {"profitable": False})),
    )

    now = datetime.now(timezone.utc)
    positions = [
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=1)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=3)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=5)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=7)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=9)),
    ]
    exec_setup.holder["cm"] = _FakeCM(positions)
    cfg = _base_cfg(ticker_learning={"enabled": False})
    ok, _sig = _run(exec_setup.agent, cfg, signal_score=80.0)
    assert ok is True, "enabled=False must not skip (no cooldown/bar)"
    assert len(exec_setup.holder["cm"].open_calls) == 1
    call = exec_setup.holder["cm"].open_calls[0]
    assert call["take_profit_pct"] == pytest.approx(0.05, rel=1e-9), (
        f"TP must be baseline 0.05 when disabled; got {call['take_profit_pct']}"
    )

    # Baseline: empty positions, enabled=True (also returns base adjustment).
    from core.execution import runtime as execution_runtime
    execution_runtime._BROKERS.clear()
    monkeypatch.setenv("MS_EXECUTION_DB_PATH", os.environ["MS_EXECUTION_DB_PATH"] + ".baseline")
    exec_setup.holder["cm"] = _FakeCM([])
    ok_base, _sig_base = _run(exec_setup.agent, cfg, signal_score=80.0, signal_id="sig-baseline")
    assert ok_base is True
    baseline_call = exec_setup.holder["cm"].open_calls[0]
    assert call["size_usd"] == pytest.approx(baseline_call["size_usd"], rel=1e-9), (
        "size must equal baseline when ticker_learning disabled"
    )
    assert call["take_profit_pct"] == pytest.approx(baseline_call["take_profit_pct"], rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# Part B — _autonomous_monitor_once stamps ticker_learning on the candidate
# ─────────────────────────────────────────────────────────────────────────────

def _signal(ticker: str, action: str = "BUY") -> dict:
    return {"ticker": ticker, "action": action, "autonomous_source": "scanner"}


@pytest.fixture
def patched_cm(monkeypatch):
    """Patch core.capital_manager.get_capital_manager (imported lazily inside
    _has_recent_close_lockout and _autonomous_monitor_once) so each test
    controls the closed-position set."""
    holder = {"cm": _FakeCM([])}

    def _factory():
        return holder["cm"]

    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        _factory,
        raising=True,
    )
    yield holder
    holder["cm"] = _FakeCM([])


def test_monitor_once_stamps_ticker_learning_on_candidate(monkeypatch, patched_cm):
    """3 CLOSED LOSS positions on ETH-USD/LONG with most recent 7h ago (outside
    the 6h lockout so the candidate reaches the stamp). _autonomous_monitor_once
    must stamp signal['ticker_learning'] with action REDUCE (streak 3) before
    on_new_signal fires."""
    now = datetime.now(timezone.utc)
    patched_cm["cm"] = _FakeCM([
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=7)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=9)),
        _loss_pos("ETH-USD", "LONG", now - timedelta(hours=11)),
    ])

    mgr = AgentManager.__new__(AgentManager)  # bypass __init__ (no engine deps)
    mgr.agents = {"execution": type("E", (), {"is_running": True})()}
    mgr._autonomous_last_decisions = {}

    monkeypatch.setattr(mgr, "_execution_config", lambda: {
        "autonomous_candidates_enabled": True,
        "autonomous_sources": {"scanner": True, "pre_earnings": False},
        "max_autonomous_candidates_per_cycle": 4,
        "max_autonomous_trades_per_cycle": 1,
        "autonomous_cooldown_minutes": 45.0,
        "autonomous_ticker_lockout_hours": 6.0,
        "autonomous_allow_scale_in": False,
        "ticker_learning": {
            "enabled": True,
            "loss_streak_reduce": 3,
            "loss_streak_tighten": 4,
            "loss_streak_block": 5,
            "block_cooldown_hours": 6,
            "win_streak_boost": 3,
            "win_streak_boost2": 5,
            "max_leverage_cap": 5,
            "size_mult_l3": 0.5,
            "size_mult_l4": 0.25,
            "min_score_l3": 90.0,
            "min_score_l4": 93.0,
            "rr_mult_w3": 1.25,
            "rr_mult_w5": 1.5,
            "baseline_min_score": 78.0,
        },
    })
    monkeypatch.setattr(mgr, "_bot_mode_active", lambda: True)
    monkeypatch.setattr(mgr, "_scanner_candidates", lambda cfg: [_signal("ETH-USD", "BUY")])

    sent: list[dict] = []
    monkeypatch.setattr(mgr, "on_new_signal", lambda s: sent.append(s))
    # Bypass market-hours / allowed-now gate.
    monkeypatch.setattr(
        AgentManager,
        "_autonomous_trade_allowed_now",
        staticmethod(lambda s: (True, "")),
    )
    monkeypatch.setattr(mgr, "_has_open_ticker_conflict", lambda s, *, allow_scale_in: False)

    mgr._autonomous_monitor_once()

    assert len(sent) == 1, (
        f"exactly one candidate must flow through to on_new_signal; got {len(sent)}"
    )
    tl = sent[0].get("ticker_learning")
    assert isinstance(tl, dict), "ticker_learning stamp must be a dict"
    assert tl.get("action") == "REDUCE", (
        f"streak-3 LOSS must yield action REDUCE; got {tl.get('action')}"
    )
    assert tl.get("streak_len") == 3, (
        f"streak_len must be 3; got {tl.get('streak_len')}"
    )
    assert tl.get("streak_type") == "LOSS", (
        f"streak_type must be LOSS; got {tl.get('streak_type')}"
    )
