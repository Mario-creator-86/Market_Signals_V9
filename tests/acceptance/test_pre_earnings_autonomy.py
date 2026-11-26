"""§51 acceptance — every action in the §41 enum is reachable from the
Pre-Earnings decision surface under the appropriate autonomy mode, and the
autonomy gate clamps order-bearing actions correctly. Uses fake providers
(no network)."""
from core.ai.decision import Action
from core.ai.provider import AIResponse
from core.pre_earnings.decision import evaluate_pre_earnings_decision


class _FP:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")


class _Critic:
    name = "fake_critic"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")


def _services(ai_text, critic_text='{"veto":false,"categories":[],"severity":0.0,"rationale":""}'):
    s = {"primary_provider": _FP(ai_text)}
    if critic_text is not None:
        s["critic_provider"] = _Critic(critic_text)
    return s


def _item():
    return {"symbol": "NVDA", "price": 850.0, "direction_bias": 40, "gap_risk": 70,
            "pre_earnings_score": 55, "pre_earnings_score_label": "MODERATE",
            "data_confidence": "HIGH", "coverage_score": 70, "coverage_label": "HIGH",
            "news_only_call": False, "expected_move_pct": 8.5,
            "historical_median_move_pct": 6.2, "session": "AMC"}


_BASE = {"ai": {"decision": {"critic_enabled": True}}}


def test_open_long_reachable_under_paper_auto():
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"thesis":"t"}'))
    assert r.decision.action is Action.OPEN_LONG


def test_open_short_reachable_under_paper_auto():
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_SHORT","ticker":"NVDA","confidence":0.7,"thesis":"t"}'))
    assert r.decision.action is Action.OPEN_SHORT


def test_hold_and_reduce_pass_through_under_paper_auto():
    for action in ("HOLD", "REDUCE"):
        cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
        r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services(f'{{"action":"{action}","ticker":"NVDA","confidence":0.6,"thesis":"t"}}'))
        assert r.decision.action.value == action


def test_watch_and_no_trade_reachable():
    for action in ("WATCH", "NO_TRADE"):
        cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
        r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services(f'{{"action":"{action}","ticker":"NVDA","confidence":0.4}}'))
        assert r.decision.action.value == action


def test_close_reachable_under_paper_auto():
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"CLOSE","ticker":"NVDA","confidence":0.6,"thesis":"t"}'))
    assert r.decision.action is Action.CLOSE


def test_analyze_and_recommend_force_watch_for_orders():
    for mode in ("ANALYZE", "RECOMMEND", "OFF"):
        cfg = {**_BASE, "autonomy": {"mode": mode}}
        r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"t"}'))
        assert r.decision.action is Action.WATCH


def test_live_auto_requires_acceptance_for_orders():
    cfg = {**_BASE, "autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                                 "paper_auto_acceptance_passed": False}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"t"}'))
    assert r.decision.action is Action.WATCH
    cfg_ok = {**_BASE, "autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                                    "paper_auto_acceptance_passed": True}}
    r2 = evaluate_pre_earnings_decision(_item(), config=cfg_ok, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"t"}'))
    assert r2.decision.action is Action.OPEN_LONG