"""Strategy Lab: data layer, the decision (statistics + research lead), the live side and the scheduler."""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest

from core.strategy_lab import data as lab_data
from core.strategy_lab import lab
from core.strategy_lab.engine import Panel, normalize_spec, spec_id
from core.strategy_lab.live import SOURCE_NAME, LabBaskets, is_lab_signal, lab_ai_is_advisory
from core.strategy_lab.review import build_prompt, parse_reply, review_market
from core.strategy_lab.runner import StrategyLabRunner, run_is_due

DAY = 86_400_000
HOUR = 3_600_000
MOMENTUM = {"family": "xs_rank", "metric": "return", "lookback": 21, "skip": 0, "direction": "follow",
            "fraction": 5, "hold": 7, "sides": "both", "gate": {"kind": "none"}}
REVERSAL = {**MOMENTUM, "direction": "against"}
LOWVOL = {**MOMENTUM, "metric": "volatility", "direction": "against"}


@pytest.fixture(autouse=True)
def _lab_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lab_data, "lab_dir", lambda: tmp_path)
    return tmp_path


def momentum_panel(market="crypto", days=420, symbols=20, seed=3):
    """Persistent winners and losers: momentum passes, reversal fails."""
    rng = np.random.default_rng(seed)
    drift = np.linspace(-0.004, 0.004, symbols)
    close = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, (days, symbols)), axis=0)
    cfg = lab_data.MARKETS[market]
    names = [f"S{j:02d}USDT" if market == "crypto" else f"S{j:02d}" for j in range(symbols)]
    return Panel(market, np.arange(20_300, 20_300 + days), names, close, np.full((days, symbols), 50e6),
                 names[10], cfg["cost"], cfg["min_dollar_volume"])


def sid(spec, market="crypto"):
    return spec_id(normalize_spec({**spec, "max_names": lab_data.MARKETS[market]["max_names"]}))


def verdicts(**by_id):
    def ask(prompt, system, model):
        return {"text": json.dumps({"verdicts": [{"id": k, "verdict": v, "confidence": 0.8, "reason": "because"} for k, v in by_id.items()],
                                    "proposals": [LOWVOL], "commentary": "Momentum holds; reversal does not."}), "model": model}
    return ask


def run(registry, ask, *, space=(MOMENTUM, REVERSAL), now_ms=None, cfg=None, panel=None, market="crypto"):
    panel = panel if panel is not None else momentum_panel(market)
    now_ms = now_ms if now_ms is not None else (int(panel.days[-1]) + 1) * DAY + 15 * 60_000
    # The live default leaves crypto momentum to the weekly basket; the tests use momentum as their passing rule.
    cfg = {"occupied_kinds": {}, **(cfg or {})}
    return lab.run_market(market, registry, lab.lab_config({"strategy_lab": cfg}), now_ms, panel=panel, ask=ask,
                          model="claude-opus-5-5", results_by_rule={}, space=list(space))


def fresh():
    return lab.load_registry()


def part(alpha_t=3.0, sharpe=2.0, mean=0.1, drawdown=-10.0):
    return {"ok": True, "days": 200, "total_pct": 50.0, "sharpe": sharpe, "t": alpha_t, "alpha_t": alpha_t, "beta": 0.0,
            "mean_daily_pct": mean, "alpha_daily_pct": mean, "max_drawdown_pct": drawdown, "quarters_positive": 5, "quarters": 6}


def crafted(monkeypatch, *rows):
    """Make the search return exactly these (spec, passes, train, holdout, full) rows, best first."""
    def fake_search(panel, specs=None, *, max_names=0):
        out = []
        for spec, ok, train, holdout, full in rows:
            norm = normalize_spec({**spec, "max_names": lab_data.MARKETS["crypto"]["max_names"]})
            out.append({"spec": norm, "id": spec_id(norm), "train": train, "holdout": holdout, "full": full,
                        "passes": ok, "train_until": 20_500})
        return out
    monkeypatch.setattr(lab, "run_search", fake_search)
    monkeypatch.setattr(lab, "persists", lambda panel, spec: True)


# ----------------------------------------------------------------- decisions
def test_a_rule_trades_only_when_it_passes_the_bar_and_the_research_lead_approves():
    registry = fresh()
    record = run(registry, verdicts(**{sid(MOMENTUM): "approve"}))

    strategy = registry["strategies"][sid(MOMENTUM)]
    assert record["rules_tested"] == 2 and record["passed"] == 1 and record["review"]["available"]
    assert strategy["status"] == "paper" and strategy["review"]["verdict"] == "approve"
    assert "passed the statistical bar and the research lead approved" in strategy["decision"]
    assert strategy["strategy_types"] == {"LONG": f"LAB_{sid(MOMENTUM).upper()}_LONG", "SHORT": f"LAB_{sid(MOMENTUM).upper()}_SHORT"}
    assert len(strategy["live"]["long"]) == 4 and len(strategy["live"]["short"]) == 4   # a fifth of 20 names
    assert sid(REVERSAL) not in registry["strategies"]                  # failed the bar: never offered for trading
    board = registry["markets"]["crypto"]
    assert board["commentary"].startswith("Momentum holds") and board["leaderboard"][0]["id"] == sid(MOMENTUM)
    assert board["proposals"] == [normalize_spec(LOWVOL)]               # tested in the next run


@pytest.mark.parametrize("verdict,status", [("reject", "rejected"), ("watch", "watch"), ("retire", "watch")])
def test_a_finalist_the_research_lead_does_not_approve_is_not_traded(verdict, status):
    registry = fresh()
    run(registry, verdicts(**{sid(MOMENTUM): verdict}))
    strategy = registry["strategies"][sid(MOMENTUM)]
    assert strategy["status"] == status and "live" not in strategy


def test_a_finalist_can_be_run_as_a_labelled_experiment_instead_of_an_approved_strategy():
    registry = fresh()
    run(registry, verdicts(**{sid(MOMENTUM): "explore"}))
    strategy = registry["strategies"][sid(MOMENTUM)]
    assert strategy["status"] == "explore" and strategy["live"]["long"]
    assert "only as a small labelled paper experiment" in strategy["decision"]

    registry = fresh()
    run(registry, verdicts(**{sid(MOMENTUM): "explore"}), cfg={"allow_exploration": False})
    assert registry["strategies"][sid(MOMENTUM)]["status"] == "watch"      # the owner switched exploration off


@pytest.mark.parametrize("reply", [{"error": "CLI missing"}, {"text": "I think it is fine."}, {"text": ""}])
def test_without_a_usable_verdict_nothing_is_traded(reply):
    registry = fresh()
    record = run(registry, lambda prompt, system, model: reply)
    strategy = registry["strategies"][sid(MOMENTUM)]
    assert not record["review"]["available"] and strategy["status"] == "watch"
    assert "waiting for the research lead" in strategy["decision"] and "live" not in strategy


def test_a_near_miss_can_run_as_one_exploration_experiment_only_on_request(monkeypatch):
    weak = {**MOMENTUM, "lookback": 10}
    crafted(monkeypatch, (weak, False, part(alpha_t=1.6), part(alpha_t=0.8, sharpe=0.5), part(alpha_t=1.7)))

    registry = fresh()
    record = run(registry, verdicts())
    assert record["passed"] == 0 and registry["strategies"] == {}       # nobody asked: nothing trades

    run(registry, verdicts(**{sid(weak): "approve"}))
    assert registry["strategies"] == {}                                 # "approve" is only for rules that passed the bar

    run(registry, verdicts(**{sid(weak): "explore"}), cfg={"allow_exploration": False})
    assert registry["strategies"] == {}                                 # exploration switched off by the owner

    run(registry, verdicts(**{sid(weak): "explore"}))
    strategy = registry["strategies"][sid(weak)]
    assert strategy["status"] == "explore" and "did not pass the statistical bar" in strategy["decision"]
    assert strategy["live"]["long"] and strategy["stats"]["passes"] is False


def test_a_weak_near_miss_is_never_offered_for_exploration(monkeypatch):
    weak = {**MOMENTUM, "lookback": 10}
    for train, holdout, full in ((part(alpha_t=0.5), part(alpha_t=0.8), part()),                 # weak in the search period
                                 (part(alpha_t=1.6), part(alpha_t=0.2), part()),                 # did not hold afterwards
                                 (part(alpha_t=1.6), part(alpha_t=0.8, mean=-0.01), part()),     # lost money afterwards
                                 (part(alpha_t=1.6), part(alpha_t=0.8), part(drawdown=-70.0))):  # ruinous drawdown
        crafted(monkeypatch, (weak, False, train, holdout, full))
        registry = fresh()
        run(registry, verdicts(**{sid(weak): "explore"}))
        assert registry["strategies"] == {}


def test_only_as_many_rules_trade_as_there_are_slots(monkeypatch):
    second = {**MOMENTUM, "metric": "high_proximity"}
    crafted(monkeypatch, (MOMENTUM, True, part(), part(), part()), (second, True, part(2.5), part(2.5), part(2.5)))

    registry = fresh()
    run(registry, verdicts(**{sid(MOMENTUM): "approve", sid(second): "approve"}))
    assert registry["strategies"][sid(MOMENTUM)]["status"] == "paper"   # the better one takes the only slot
    waiting = registry["strategies"][sid(second)]
    assert waiting["status"] == "watch" and "waiting for a free trading slot" in waiting["decision"] and "live" not in waiting

    registry = fresh()
    run(registry, verdicts(**{sid(MOMENTUM): "approve", sid(second): "approve"}), cfg={"max_trading_per_market": 2})
    assert {s["status"] for s in registry["strategies"].values()} == {"paper"}


def test_a_kind_of_rule_already_traded_elsewhere_is_left_alone():
    registry = fresh()
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}), cfg={"occupied_kinds": {"crypto": [["return", "follow", "both"]]}})
    assert registry["strategies"] == {}                                 # the weekly momentum basket already does this


def test_a_trading_rule_is_retired_when_the_evidence_fades_or_the_research_lead_says_so(monkeypatch):
    good = (MOMENTUM, True, part(), part(), part())
    faded = (MOMENTUM, False, part(), part(alpha_t=0.3), part(alpha_t=0.9))
    registry = fresh()
    crafted(monkeypatch, good)
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}))
    strategy = registry["strategies"][sid(MOMENTUM)]
    assert strategy["status"] == "paper"

    crafted(monkeypatch, faded)
    run(registry, verdicts()); run(registry, verdicts())
    assert strategy["status"] == "paper" and strategy["fail_streak"] == 2   # two weak runs are not yet a verdict
    crafted(monkeypatch, good)
    run(registry, verdicts())
    assert strategy["fail_streak"] == 0                                 # the evidence came back: the count starts again
    crafted(monkeypatch, faded)
    for _ in range(3):
        run(registry, verdicts())
    assert strategy["status"] == "retired" and "did not hold in 3 consecutive runs" in strategy["decision"]
    assert "live" not in strategy                                       # nothing new is opened

    registry = fresh()
    crafted(monkeypatch, good)
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}))
    run(registry, verdicts(**{sid(MOMENTUM): "retire"}))
    strategy = registry["strategies"][sid(MOMENTUM)]
    assert strategy["status"] == "retired" and strategy["decision"].startswith("retired by the research lead")
    assert [h["to"] for h in strategy["history"]] == ["paper", "retired"]


def test_a_rejected_rule_is_not_brought_back_before_the_cooldown():
    registry = fresh()
    panel = momentum_panel()
    first = (int(panel.days[-1]) + 1) * DAY
    run(registry, verdicts(**{sid(MOMENTUM): "reject"}), now_ms=first)
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}), now_ms=first + 5 * DAY)
    assert registry["strategies"][sid(MOMENTUM)]["status"] == "rejected"
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}), now_ms=first + 31 * DAY)
    assert registry["strategies"][sid(MOMENTUM)]["status"] == "paper"


def test_the_research_leads_proposals_are_tested_like_any_other_rule():
    registry = fresh()
    registry["markets"]["crypto"] = {"proposals": [{**MOMENTUM, "lookback": 28}, {"family": "python", "code": "import os"}]}
    record = run(registry, verdicts(), space=(REVERSAL,))
    assert record["rules_tested"] == 2                                  # the valid proposal ran, the other was dropped
    origins = {row["id"]: row["origin"] for row in registry["markets"]["crypto"]["leaderboard"]}
    assert origins[sid({**MOMENTUM, "lookback": 28})] == "research lead"


# ------------------------------------------------------------- live schedule
def test_crypto_members_are_entered_after_midnight_and_closed_before_the_next_ranking():
    panel = momentum_panel()
    as_of = int(panel.days[-1])
    plan = lab.live_plan("crypto", panel, normalize_spec({**MOMENTUM, "max_names": 3}), (as_of + 1) * DAY + 900_000,
                         lab.lab_config(None))
    assert plan["as_of_day"] == as_of and len(plan["long"]) == 3 and len(plan["short"]) == 3
    assert plan["enter_from_ms"] == (as_of + 1) * DAY
    assert plan["enter_until_ms"] == (as_of + 1) * DAY + 12 * HOUR
    assert plan["exit_at_ms"] == (as_of + 8) * DAY - HOUR               # 7 days held, out an hour before the re-ranking
    assert set(plan["adv_usd"]) == set(plan["long"] + plan["short"]) and all(v == 50e6 for v in plan["adv_usd"].values())
    assert plan["next_rebalance_day"] == as_of + 7


def test_stock_members_are_entered_at_the_next_open_and_closed_before_the_last_close():
    friday = (dt.date(2026, 11, 20) - dt.date(1970, 1, 1)).days
    panel = momentum_panel("stocks")
    panel.days = np.arange(friday - panel.shape[0] + 1, friday + 1)
    plan = lab.live_plan("stocks", panel, normalize_spec({**MOMENTUM, "hold": 5, "max_names": 6}), (friday + 1) * DAY,
                         lab.lab_config(None))
    enter = dt.datetime.fromtimestamp(plan["enter_from_ms"] / 1000, lab.NY)
    leave = dt.datetime.fromtimestamp(plan["exit_at_ms"] / 1000, lab.NY)
    assert (enter.date(), enter.hour, enter.minute) == (dt.date(2026, 11, 23), 9, 35)      # Monday's open
    # Thanksgiving week: Mon, Tue, Wed, (Thu closed), Fri, Mon -> the fifth session is the 30th.
    assert (leave.date(), leave.hour, leave.minute) == (dt.date(2026, 11, 30), 15, 45)
    short_day = lab.live_plan("stocks", panel, normalize_spec({**MOMENTUM, "hold": 4, "max_names": 6}), (friday + 1) * DAY,
                              lab.lab_config(None))
    leave = dt.datetime.fromtimestamp(short_day["exit_at_ms"] / 1000, lab.NY)
    assert (leave.date(), leave.hour, leave.minute) == (dt.date(2026, 11, 27), 12, 45)     # the Friday session ends at 13:00
    assert plan["enter_until_ms"] - plan["enter_from_ms"] == 30 * HOUR
    assert lab.trading_ordinal("stocks", friday + 3) == lab.trading_ordinal("stocks", friday) + 1   # the weekend does not count

    wednesday = (dt.date(2026, 11, 25) - dt.date(1970, 1, 1)).days                          # the day before Thanksgiving
    panel.days = np.arange(wednesday - panel.shape[0] + 1, wednesday + 1)
    plan = lab.live_plan("stocks", panel, normalize_spec({**MOMENTUM, "hold": 3, "max_names": 6}), (wednesday + 1) * DAY,
                         lab.lab_config(None))
    enter = dt.datetime.fromtimestamp(plan["enter_from_ms"] / 1000, lab.NY)
    leave = dt.datetime.fromtimestamp(plan["exit_at_ms"] / 1000, lab.NY)
    assert enter.date() == dt.date(2026, 11, 27)                                            # Thursday is a holiday
    assert leave.date() == dt.date(2026, 12, 1)                                             # Friday, Monday, Tuesday


def test_members_stay_until_the_next_ranking_is_due():
    registry = fresh()
    panel = momentum_panel()
    first = (int(panel.days[-1]) + 1) * DAY
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}), panel=panel, now_ms=first)
    before = dict(registry["strategies"][sid(MOMENTUM)]["live"])

    later = momentum_panel(days=423)                                    # three more days: hold is seven
    registry["strategies"][sid(MOMENTUM)]["live"].pop("adv_usd")        # a plan written before liquidity was recorded
    run(registry, verdicts(), panel=later, now_ms=first + 3 * DAY)
    assert registry["strategies"][sid(MOMENTUM)]["live"] == before      # same members; the liquidity figures are filled in

    much_later = momentum_panel(days=427)
    run(registry, verdicts(), panel=much_later, now_ms=first + 7 * DAY)
    assert registry["strategies"][sid(MOMENTUM)]["live"]["as_of_day"] == before["as_of_day"] + 7


def test_a_ranking_that_came_too_late_to_enter_is_redone_on_the_next_run():
    registry = fresh()
    panel = momentum_panel()
    late = (int(panel.days[-1]) + 1) * DAY + 15 * HOUR                  # the 12-hour entry window is over
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}), panel=panel, now_ms=late)
    live = registry["strategies"][sid(MOMENTUM)]["live"]
    assert live["missed_window"] is True and live["next_rebalance_day"] == int(panel.days[-1]) + 1

    next_day = momentum_panel(days=421)
    run(registry, verdicts(), panel=next_day, now_ms=(int(next_day.days[-1]) + 1) * DAY + 15 * 60_000)
    live = registry["strategies"][sid(MOMENTUM)]["live"]
    assert "missed_window" not in live and live["as_of_day"] == int(next_day.days[-1])
    assert live["next_rebalance_day"] == int(next_day.days[-1]) + 7


# ------------------------------------------------------------------- review
def test_the_reply_is_parsed_defensively():
    reply = ('Here you go:\n{"verdicts": [{"id": "abc", "verdict": "APPROVE", "confidence": 7, "reason": "ok"}, '
             '{"id": "zzz", "verdict": "approve"}, {"id": "abc2", "verdict": "buy now"}], '
             '"proposals": [' + json.dumps(MOMENTUM) + ', {"family": "shell"}, "x"], "commentary": "fine"}')
    parsed = parse_reply(reply, ["abc", "abc2"])
    assert parsed["verdicts"] == {"abc": {"verdict": "approve", "confidence": 1.0, "reason": "ok"}}
    assert parsed["proposals"] == [normalize_spec(MOMENTUM)] and parsed["commentary"] == "fine"
    assert parse_reply("no json here", ["abc"]) is None and parse_reply("[1, 2]", ["abc"]) is None


def test_the_prompt_carries_the_evidence_and_no_reply_means_unavailable():
    seen = {}

    def ask(prompt, system, model):
        seen.update(prompt=prompt, system=system, model=model)
        return {"error": "daily call limit reached"}

    registry = fresh()
    run(registry, ask)
    assert "finalists_passed_the_bar" in seen["prompt"] and sid(MOMENTUM) in seen["prompt"]
    assert "multiple_testing_warning" in seen["prompt"] and seen["model"] == "claude-opus-5-5"
    assert "Simulated money only" in seen["system"]
    assert review_market("crypto", finalists=[], near_misses=[], trading=[], summary={}, ask=ask)["error"] == "nothing to review"


def test_when_nothing_came_close_the_research_lead_still_proposes_what_to_test_next(monkeypatch):
    far = {**MOMENTUM, "lookback": 10}
    crafted(monkeypatch, (far, False, part(alpha_t=0.2), part(alpha_t=0.1), part(alpha_t=0.2)))
    seen = {}

    def ask(prompt, system, model):
        seen["prompt"] = prompt
        return {"text": json.dumps({"verdicts": [{"id": sid(far), "verdict": "approve", "confidence": 1, "reason": "x"}],
                                    "proposals": [LOWVOL], "commentary": "Nothing works yet."}), "model": model}

    registry = fresh()
    record = run(registry, ask)
    assert record["review"]["available"] and "best_tested_rules_far_from_the_bar" in seen["prompt"]
    assert registry["strategies"] == {}                                 # a rule far from the bar cannot be approved into trading
    board = registry["markets"]["crypto"]
    assert board["proposals"] == [normalize_spec(LOWVOL)] and board["commentary"] == "Nothing works yet."
    assert "rule_format" in build_prompt("stocks", finalists=[], near_misses=[], trading=[], summary={})


# --------------------------------------------------------------------- data
def test_price_tables_round_trip_and_the_panel_drops_what_was_never_liquid(tmp_path):
    table = {"AAA": {d: (10.0 + d % 3, 30e6) for d in range(20_000, 20_300)},
             "BBB": {d: (5.0 + d % 2, 25e6) for d in range(20_000, 20_300)},
             "THIN": {d: (1.0, 1e5) for d in range(20_000, 20_300)},
             "SPY": {d: (500.0 + d % 5, 9e9) for d in range(20_000, 20_300)}}
    table.update({f"X{i:02d}": {d: (20.0 + i, 40e6) for d in range(20_000, 20_300)} for i in range(20)})
    path = tmp_path / "t.npz"
    lab_data.save_table(path, table)
    assert lab_data.load_table(path)["AAA"][20_150] == (10.0 + 20_150 % 3, 30e6)
    panel = lab_data.panel_from_table("stocks", lab_data.load_table(path), last_day=20_250)
    assert int(panel.days[-1]) == 20_250 and "THIN" not in panel.symbols and "SPY" in panel.symbols
    assert panel.entry_lag == 1 and panel.borrow_daily == pytest.approx(0.20 / 252)   # stocks: next-session entry, borrow fee
    assert lab_data.panel_from_table("stocks", {"AAA": table["AAA"]}) is None        # too little to rank
    assert lab_data.load_table(tmp_path / "missing.npz") == {}


def test_only_completed_days_count():
    monday_noon_ny = int(dt.datetime(2026, 11, 23, 12, 0, tzinfo=lab_data.NY).timestamp() * 1000)
    monday_evening_ny = int(dt.datetime(2026, 11, 23, 17, 0, tzinfo=lab_data.NY).timestamp() * 1000)
    friday = (dt.date(2026, 11, 20) - dt.date(1970, 1, 1)).days
    assert lab_data.last_complete_day("stocks", monday_noon_ny) == friday            # the session is still open
    assert lab_data.last_complete_day("stocks", monday_evening_ny) == friday + 3
    assert lab_data.last_complete_day("crypto", (friday + 3) * DAY + 5) == friday + 2  # the UTC day that just ended
    thanksgiving_evening = int(dt.datetime(2026, 11, 26, 18, 0, tzinfo=lab_data.NY).timestamp() * 1000)
    assert lab_data.last_complete_day("stocks", thanksgiving_evening) == friday + 5  # Wednesday: the holiday had no session
    assert lab_data.session_after(dt.date(2026, 11, 25), 1) == dt.date(2026, 11, 27)


class FakeBinance:
    def __init__(self, now_day):
        self.now_day, self.calls = now_day, []

    def __call__(self, url):
        self.calls.append(url)
        if "ticker/24hr" in url:
            return 200, ([{"symbol": f"C{i:02d}USDT", "quoteVolume": str(1e8 - i)} for i in range(25)]
                         + [{"symbol": "USDCUSDT", "quoteVolume": "9e9"}, {"symbol": "NOPERPUSDT", "quoteVolume": "9e9"},
                            {"symbol": "PEPEUSDT", "quoteVolume": "5e9"}])
        if "exchangeInfo" in url:
            names = [f"C{i:02d}USDT" for i in range(25)] + ["USDCUSDT", "1000PEPEUSDT"]
            return 200, {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"} for s in names]}
        if "klines" in url:
            return 200, [[d * DAY, "0", "0", "0", "10", "0", (d + 1) * DAY - 1, "20000000"]
                         for d in range(self.now_day - 9, self.now_day + 1)]
        if "fundingRate" in url:
            start = int(url.split("startTime=")[1].split("&")[0]) // DAY
            return 200, [{"fundingTime": d * DAY + h * HOUR, "fundingRate": "0.0001"}
                         for d in range(max(start, self.now_day - 3), self.now_day + 1) for h in (0, 8, 16)]
        raise AssertionError(url)


def test_crypto_refresh_keeps_shortable_coins_and_drops_the_running_day():
    api = FakeBinance(now_day=20_780)
    status = lab_data.refresh_crypto(api, now_ms=20_780 * DAY + HOUR)
    table = lab_data.load_table(lab_data.lab_dir() / "crypto_daily.npz")
    assert status["ok"] and status["symbols"] == 26 and status["last_day"] == 20_779
    assert "USDCUSDT" not in table and "NOPERPUSDT" not in table and "PEPEUSDT" in table   # 1000PEPE is its perpetual
    assert max(table["C00USDT"]) == 20_779 and table["C00USDT"][20_779] == (10.0, 20e6)


def test_funding_is_summed_per_day_and_continues_where_it_stopped():
    api = FakeBinance(now_day=20_780)
    lab_data.refresh_crypto(api, now_ms=20_780 * DAY + HOUR)
    status = lab_data.refresh_funding(api, now_ms=20_780 * DAY + HOUR, sleep=lambda s: None)
    table = lab_data.load_table(lab_data.lab_dir() / "crypto_funding.npz")
    assert status["ok"] and status["complete"] and table["C00USDT"][20_778][0] == pytest.approx(0.0003)
    assert any("1000PEPEUSDT" in url for url in api.calls)
    before = len(api.calls)
    lab_data.refresh_funding(api, now_ms=20_780 * DAY + HOUR, sleep=lambda s: None)
    assert len(api.calls) == before + 1                                 # only the list of perpetuals: all up to date
    panel = lab_data.panel_from_table("crypto", {s: {d: (10.0, 20e6) for d in range(20_400, 20_780)} for s in table},
                                      carry_table=table)
    assert panel.carry[-2, panel.symbols.index("C00USDT")] == pytest.approx(0.0003)


def test_stock_refresh_is_slow_small_and_survives_a_split():
    friday = (dt.date(2026, 11, 20) - dt.date(1970, 1, 1)).days
    now = int(dt.datetime(2026, 11, 23, 12, 0, tzinfo=lab_data.NY).timestamp() * 1000)
    old = {d: (100.0, 50e6) for d in range(friday - 300, friday - 1)}
    lab_data.save_table(lab_data.lab_dir() / "stocks_daily.npz", {"AAA": dict(old), "SPLT": dict(old), "SPY": dict(old)})
    calls, pauses = [], []

    def download(symbols, period):
        calls.append((tuple(symbols), period))
        out = {}
        for s in symbols:
            days = range(friday - 20, friday + 2) if period == "1mo" else range(friday - 400, friday + 2)
            price = 50.0 if s == "SPLT" else 100.0                       # SPLT split two for one
            out[s] = [(d, price, 50e6) for d in days]
        return out

    status = lab_data.refresh_stocks(add=["NEW1", "aaa"], download=download, now_ms=now, batch=2, sleep=pauses.append)
    table = lab_data.load_table(lab_data.lab_dir() / "stocks_daily.npz")
    assert status["ok"] and max(table["AAA"]) == friday                  # the running session was not stored
    assert table["SPLT"][friday - 100] == (50.0, 50e6)                   # re-based history replaced the old one
    assert min(table["NEW1"]) == friday - 400 and status["added"] == 1
    assert calls[0] == (("AAA", "SPLT"), "1mo") and (("SPLT",), "5y") in calls and (("NEW1",), "5y") in calls
    assert pauses and all(p == 4.0 for p in pauses)

    stopped = lab_data.refresh_stocks(add=["NEW2"], download=lambda symbols, period: {}, now_ms=now + 4 * DAY, sleep=pauses.append)
    assert not stopped["ok"] and "stopped to stay under Yahoo's limits" in stopped["reason"]


DIRECTORY = """Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares
Y|AAPL|Apple Inc. - Common Stock|Q|Q|N|40|N|N||AAPL|N
Y|BRK.B|Berkshire Hathaway Inc. New Common Stock|N| |N|40|N||BRK.B|BRK.B|N
Y|SOXL|Direxion Daily Semiconductor Bull 3X ETF|P| |Y|100|N||SOXL|SOXL|N
Y|SPY|State Street SPDR S&P 500 ETF Trust|P| |Y|40|N||SPY|SPY|N
Y|ABCDW|Abcd Corp. - Warrant|Q|G|N|100|N|N||ABCDW|N
Y|XYZU|Xyz Acquisition Corp. Unit|N| |N|100|N||XYZU|XYZU|N
Y|ZTEST|Test Company Common Stock|Q|Q|N|100|Y|N||ZTEST|N
""" + "".join(f"Y|F{i:04d}|Filler {i} Inc. Common Stock|N| |N|100|N||F{i:04d}|F{i:04d}|N\n" for i in range(1000)) + "File Creation Time: 1123202610:03|||||\n"


def test_only_ordinary_shares_are_ranked():
    meta = lab_data.parse_symbol_directory(DIRECTORY)
    assert meta["AAPL"]["rankable"] and meta["BRK-B"]["rankable"]         # Yahoo spells the class with a dash
    assert meta["SOXL"]["etf"] and not meta["SOXL"]["rankable"]            # a 3x ETF is not a stock
    assert not meta["ABCDW"]["rankable"] and not meta["XYZU"]["rankable"] and not meta["ZTEST"]["rankable"]
    assert lab_data.parse_symbol_directory("garbage") == {} and lab_data.parse_symbol_directory("") == {}

    rows = {1: (10.0, 1e6)}
    table = {"AAPL": rows, "SOXL": rows, "SPY": rows, "UNKNOWN": rows}
    assert sorted(lab_data.rankable_stocks(table, meta, "SPY")) == ["AAPL", "SPY"]   # the benchmark stays, as a benchmark
    assert sorted(lab_data.rankable_stocks(table, {}, "SPY")) == ["SPY"]            # no directory: nothing is ranked


def test_the_symbol_directory_is_downloaded_once_a_week():
    calls = []

    def fetch_text(url):
        calls.append(url)
        return 200, DIRECTORY

    now = 20_780 * DAY
    first = lab_data.refresh_stock_meta(fetch_text, now)
    assert first == {"ok": True, "symbols": 1007, "refreshed": True}
    assert lab_data.refresh_stock_meta(fetch_text, now + 3 * DAY)["refreshed"] is False and len(calls) == 1
    assert lab_data.refresh_stock_meta(fetch_text, now + 8 * DAY)["refreshed"] is True and len(calls) == 2
    failed = lab_data.refresh_stock_meta(lambda url: (0, "timeout"), now + 20 * DAY)
    assert failed["ok"] is False and failed["symbols"] == 1007             # the stored copy stays in use
    assert lab_data.load_stock_meta()["SOXL"]["etf"] is True
    assert lab_data._http_text("https://example.invalid/")[0] == 0         # no network inside the test suite


def test_the_real_downloads_are_switched_off_inside_the_test_suite():
    assert lab_data._yahoo_download(["AAPL"], "1mo") == {}
    from core.strategy_lab.live import _yahoo_quotes

    assert _yahoo_quotes(["AAPL"]) == {}


# ---------------------------------------------------------------- live side
T0 = 20_780 * DAY


def write_registry(tmp_path, **overrides):
    live = {"as_of_day": 20_779, "as_of_date": "2026-11-22", "enter_from_ms": T0, "enter_until_ms": T0 + 12 * HOUR,
            "exit_at_ms": T0 + 7 * DAY - HOUR, "exit_at": "x", "long": ["ETHUSDT", "SOLUSDT"], "short": ["DOGEUSDT"],
            "adv_usd": {"ETHUSDT": 9e9, "DOGEUSDT": 8e8, "AAPL": 1.2e10}}
    crypto = {"id": "abc123", "market": "crypto", "status": "paper", "description": "crypto: rank by 14-day return",
              "strategy_types": {"LONG": "LAB_ABC123_LONG", "SHORT": "LAB_ABC123_SHORT"}, "live": live,
              "stats": {"passes": True, "full": {"sharpe": 1.4}}, "review": {"verdict": "approve", "confidence": 0.7}}
    stocks = {"id": "def456", "market": "stocks", "status": "explore", "description": "stocks: rank by 21-day volatility",
              "strategy_types": {"LONG": "LAB_DEF456_LONG", "SHORT": "LAB_DEF456_SHORT"},
              "live": {**live, "long": ["AAPL"], "short": ["TSLA"]}, "stats": {"passes": False}}
    watching = {"id": "ghi789", "market": "crypto", "status": "watch", "live": live}
    registry = {"strategies": {"abc123": crypto, "def456": stocks, "ghi789": watching}, **overrides}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    return path


def baskets(tmp_path, clock, *, prices=None, quotes=None, path=None):
    prices = {"ETHUSDT": 3000.0, "SOLUSDT": 150.0, "DOGEUSDT": 0.2} if prices is None else prices
    quote_calls = []

    def stock_quotes(symbols):
        quote_calls.append(list(symbols))
        return {"AAPL": 200.0, "TSLA": 400.0} if quotes is None else quotes

    lab_baskets = LabBaskets(path or write_registry(tmp_path), tmp_path / "live_state.json",
                             fetch=lambda url: (200, [{"symbol": s, "price": str(p)} for s, p in prices.items()]),
                             stock_quotes=stock_quotes, now_ms=lambda: clock["now"],
                             market_open=lambda: clock.get("market_open", True))
    lab_baskets.quote_calls = quote_calls
    return lab_baskets


def test_trading_rules_become_candidates_inside_the_entry_window(tmp_path):
    clock = {"now": T0 + HOUR}
    signals = baskets(tmp_path, clock).candidates({"mode": "paper"})

    by_ticker = {s["ticker"]: s for s in signals}
    assert sorted(by_ticker) == ["AAPL", "DOGE-USD", "ETH-USD", "SOL-USD", "TSLA"]      # the watched rule offers nothing
    eth, doge, aapl = by_ticker["ETH-USD"], by_ticker["DOGE-USD"], by_ticker["AAPL"]
    assert (eth["action"], eth["strategy_type"], eth["asset_class"]) == ("BUY", "LAB_ABC123_LONG", "CRYPTO_FUTURES")
    assert (doge["action"], doge["strategy_type"]) == ("SHORT", "LAB_ABC123_SHORT")
    assert (aapl["action"], aapl["strategy_type"], aapl["asset_class"]) == ("BUY", "LAB_DEF456_LONG", "US_STOCKS")
    assert eth["entry"] == 3000.0 and eth["sl"] == 2250.0 and doge["sl"] == pytest.approx(0.25)
    assert eth["time_stop_minutes"] == (7 * 24 - 2) * 60                # until an hour before the next ranking
    assert eth["autonomous_source"] == SOURCE_NAME and eth["entry_strategy"] == "lab_abc123"
    assert eth["regime_evidence"]["passes_the_bar"] is True and "exploration experiment" in aapl["reasons"][3]
    assert min(s["signal_score"] for s in signals) >= 92.5              # above the bot's minimum edge score
    assert eth["id"] == "auto_lab_abc123_ETHUSDT_long_20779"
    # The Risk Constitution refuses a candidate whose liquidity cannot be shown (seen live: "ADV unavailable").
    assert eth["average_daily_volume_usd"] == 9e9 and aapl["average_daily_volume_usd"] == 1.2e10
    assert by_ticker["SOL-USD"]["average_daily_volume_usd"] is None      # unknown stays unknown, never invented


def test_nothing_is_offered_outside_the_window_or_without_a_price(tmp_path):
    clock = {"now": T0 - 5}
    lab_baskets = baskets(tmp_path, clock, prices={"ETHUSDT": 3000.0})
    assert lab_baskets.candidates({}) == []                              # before the window
    clock["now"] = T0 + HOUR
    assert sorted(s["ticker"] for s in lab_baskets.candidates({})) == ["AAPL", "ETH-USD", "TSLA"]
    clock["now"] = T0 + 13 * HOUR
    assert lab_baskets.candidates({}) == []                              # after the window
    assert lab_baskets.candidates({SOURCE_NAME: {"enabled": False}}) == []
    assert lab_baskets.status()["trading_rules"][0]["phase"] == "holding"


def test_a_member_is_not_retried_forever_and_stock_quotes_are_cached(tmp_path):
    clock = {"now": T0 + HOUR}
    lab_baskets = baskets(tmp_path, clock)
    first = lab_baskets.candidates({})
    for _ in range(3):
        lab_baskets.note_reviewed("auto_lab_abc123_ETHUSDT_long_20779")
    lab_baskets.note_reviewed("auto_xsmom_ETHUSDT_long_20261123")        # not a lab signal: ignored
    clock["now"] += 60_000
    again = baskets(tmp_path, clock, path=tmp_path / "registry.json")   # the count survives a restart
    assert len(again.candidates({})) == len(first) - 1
    lab_baskets.candidates({})
    assert len(lab_baskets.quote_calls) == 1                             # quotes are reused for five minutes
    clock["now"] += 6 * 60_000
    lab_baskets.candidates({})
    assert len(lab_baskets.quote_calls) == 2


def test_stocks_are_neither_quoted_nor_offered_while_the_market_is_closed(tmp_path):
    clock = {"now": T0 + HOUR, "market_open": False}
    lab_baskets = baskets(tmp_path, clock)
    assert sorted(s["ticker"] for s in lab_baskets.candidates({})) == ["DOGE-USD", "ETH-USD", "SOL-USD"]   # crypto trades on
    assert lab_baskets.quote_calls == []
    clock["market_open"] = True
    assert "AAPL" in {s["ticker"] for s in lab_baskets.candidates({})}


def test_a_missing_or_half_written_registry_offers_nothing(tmp_path):
    clock = {"now": T0 + HOUR}
    assert baskets(tmp_path, clock, path=tmp_path / "nope.json").candidates({}) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert baskets(tmp_path, clock, path=broken).candidates({}) == []


def test_lab_signals_are_basket_rules_advisory_ai_in_paper_only_and_paper_capital_only(tmp_path):
    from agents.execution_agent import basket_ticker_adjustment
    from core.capital_entry_guard import evaluate_capital_entry
    import core.agent_manager as am

    signal = baskets(tmp_path, {"now": T0 + HOUR}).candidates({})[0]
    assert is_lab_signal(signal) and not is_lab_signal({"strategy_type": "SCANNER_AI_LONG"})
    assert lab_ai_is_advisory(signal, {"mode": "paper"}) and not lab_ai_is_advisory(signal, {"mode": "live"})
    assert not lab_ai_is_advisory(signal, {"mode": "paper", SOURCE_NAME: {"ai_advisory": False}})
    assert am._ai_block_is_advisory(signal, {"mode": "paper"}) and not am._ai_block_is_advisory(signal, {"mode": "live"})
    streak = {"action": "REDUCE", "size_mult": 0.5}
    assert basket_ticker_adjustment(signal, streak) == (None, streak)

    signal.update({"_autonomous_candidate": True, "price_verified": True,
                   "price_integrity": {"status": "verified", "is_verified": True}})
    paper = evaluate_capital_entry(signal, [], {"mode": "paper"})
    assert paper["allowed"] and paper["check"] == "paper_autonomous_experiment" and "Factor rule" in paper["reason"]
    assert evaluate_capital_entry(signal, [], {"mode": "live"})["allowed"] is False


def test_agent_manager_collects_lab_candidates_and_counts_reviews(monkeypatch):
    import core.agent_manager as am

    class Stub:
        reviewed = []

        def candidates(self, cfg, exclude=None):
            assert exclude("ETHUSDT") is False
            return [{"id": "auto_lab_abc123_ETHUSDT_long_20779", "ticker": "ETH-USD", "autonomous_source": SOURCE_NAME}]

        def status(self):
            return {"last_error": "", "trading_rules": [{"id": "abc123"}]}

        def note_reviewed(self, signal_id):
            self.reviewed.append(signal_id)

    stub = Stub()
    monkeypatch.setattr(am, "get_lab_baskets", lambda: stub)
    monkeypatch.setattr(am, "autonomous_instrument_exclusion", lambda candidate, policy=None, tags=None: None)
    manager = am.AgentManager.__new__(am.AgentManager)
    manager._autonomous_source_stats = {}
    monkeypatch.setattr(manager, "_instrument_exclusion_inputs", lambda: ({}, {}))

    found = manager._strategy_lab_candidates({})
    assert [c["ticker"] for c in found] == ["ETH-USD"]
    assert manager._autonomous_source_stats[SOURCE_NAME]["candidate_count"] == 1
    manager._note_autonomous_review(found[0])
    assert stub.reviewed == ["auto_lab_abc123_ETHUSDT_long_20779"]


# ----------------------------------------------------------------- scheduler
def test_a_run_is_due_once_a_day_after_the_configured_time():
    cfg = lab.lab_config(None)
    day = 20_780
    assert run_is_due({}, cfg, day * DAY + 5)                            # never ran: at once
    assert not run_is_due({"last_run_day": day}, cfg, day * DAY + 20 * HOUR)
    assert not run_is_due({"last_run_day": day - 1}, cfg, day * DAY + 10 * 60_000)   # 00:10 UTC: too early
    assert run_is_due({"last_run_day": day - 1}, cfg, day * DAY + 16 * 60_000)       # 00:16 UTC


def test_the_scheduler_starts_one_run_and_backs_off_after_a_failure():
    clock = {"now": 20_780 * DAY + HOUR}
    outcomes = [{"ok": False, "detail": "boom"}, {"ok": True, "detail": "done"}]
    spawned = []

    def spawn():
        spawned.append(clock["now"])
        return outcomes[len(spawned) - 1]

    runner = StrategyLabRunner(lambda: {}, spawn=spawn, now_ms=lambda: clock["now"])
    assert runner.tick()["ok"] is False and len(spawned) == 1
    clock["now"] += 10 * 60_000
    assert runner.tick() is None                                        # failed ten minutes ago: wait
    clock["now"] += 60 * 60_000
    assert runner.tick()["ok"] is True and len(spawned) == 2
    status = runner.status()
    assert status["last_result"]["detail"] == "done" and status["daily_run_after_utc"] == "00:15"
    assert StrategyLabRunner(lambda: {"strategy_lab": {"enabled": False}}, spawn=spawn, now_ms=lambda: clock["now"]).tick() is None
    assert runner.start() is False                                      # the scheduler thread never starts under test


def test_the_whole_run_saves_a_registry_the_ui_can_read(monkeypatch):
    panel = momentum_panel()
    monkeypatch.setattr(lab_data, "load_panel", lambda market, now_ms=None: panel if market == "crypto" else None)
    monkeypatch.setattr(lab, "default_space", lambda cap=0, min_hold=1: [normalize_spec({**MOMENTUM, "max_names": cap})])
    monkeypatch.setattr(lab, "paper_results", lambda db_path=None: {})
    now = (int(panel.days[-1]) + 1) * DAY + HOUR
    registry = lab.run_once(now, config={"ai": {"assignment": {"research_scientist": "claude-opus-5-5"}},
                                         "strategy_lab": {"occupied_kinds": {}}},
                            refresh=False, ask=verdicts(**{sid(MOMENTUM): "approve"}))
    saved = lab.load_registry()
    assert saved["last_run_day"] == now // DAY and [r["market"] for r in saved["runs"]] == ["crypto", "stocks"]
    assert saved["runs"][1]["error"] == "not enough price history yet"
    assert registry["strategies"][sid(MOMENTUM)]["status"] == "paper"

    from api import strategy_lab_service

    monkeypatch.setattr(strategy_lab_service, "_config", lambda: lab.lab_config(None))
    monkeypatch.setattr(strategy_lab_service, "paper_results", lambda: {sid(MOMENTUM): {"closed": 2, "open": 1}})
    state = strategy_lab_service.strategy_lab_state()
    row = state["strategies"][0]
    assert row["status"] == "paper" and row["paper_results"] == {"closed": 2, "open": 1} and len(row["live"]["long"]) == 4
    assert state["markets"]["crypto"]["trading"] == 1 and state["runner"]["run_in_progress"] is False


# ------------------------------------------------ findings of the independent review (2026-11-23)
def test_a_pass_must_last_a_rule_that_passes_only_on_the_latest_data_is_not_a_finalist(monkeypatch):
    crafted(monkeypatch, (MOMENTUM, True, part(), part(), part()))
    monkeypatch.setattr(lab, "persists", lambda panel, spec: False)       # it passed today, not 5, 10, 21 bars ago
    seen = {}

    def ask(prompt, system, model):
        seen["prompt"] = prompt
        return {"text": json.dumps({"verdicts": [{"id": sid(MOMENTUM), "verdict": "approve", "confidence": 0.9, "reason": "x"}]}),
                "model": model}

    registry = fresh()
    record = run(registry, ask)
    assert record["passed"] == 1 and record["passed_today_only"] == 1
    assert registry["strategies"] == {}                                 # "approve" cannot promote a blip
    assert '"passed_only_on_the_latest_data": true' in seen["prompt"]

    run(registry, verdicts(**{sid(MOMENTUM): "explore"}))               # it may still be explored, on request
    assert registry["strategies"][sid(MOMENTUM)]["status"] == "explore"


def test_the_persistence_check_reruns_the_rule_on_the_history_as_it_stood_earlier():
    panel = momentum_panel()
    good = normalize_spec({**MOMENTUM, "max_names": 5})
    bad = normalize_spec({**REVERSAL, "max_names": 5})
    assert lab.persists(panel, good) is True and lab.persists(panel, bad) is False
    assert lab.persists(momentum_panel(days=260), good) is False        # too little history left to re-test


def test_a_half_refreshed_day_is_never_ranked_into_a_basket():
    registry = fresh()
    panel = momentum_panel()
    first = (int(panel.days[-1]) + 1) * DAY
    run(registry, verdicts(**{sid(MOMENTUM): "approve"}), panel=panel, now_ms=first)
    assert lab.last_row_complete(panel) is True

    week_later = momentum_panel(days=427)
    week_later.close[-1, 5:] = np.nan                                   # only the first symbols arrived for the last day
    broken = Panel("crypto", week_later.days, week_later.symbols, week_later.close, week_later.dollar_volume,
                   week_later.benchmark, week_later.cost, 10e6)
    assert lab.last_row_complete(broken) is False
    record = run(registry, verdicts(), panel=broken, now_ms=first + 7 * DAY)
    live = registry["strategies"][sid(MOMENTUM)]["live"]
    assert record["incomplete_data"] is True and live["incomplete_data"] is True
    assert live["long"] == [] and live["short"] == []                   # nothing is offered from a partial ranking
    assert live["next_rebalance_day"] == int(broken.days[-1]) + 1       # ranked again on the next run


def test_a_holiday_inside_the_holding_period_does_not_bring_the_next_ranking_forward():
    tuesday = (dt.date(2026, 11, 24) - dt.date(1970, 1, 1)).days
    panel = momentum_panel("stocks")
    panel.days = np.arange(tuesday - panel.shape[0] + 1, tuesday + 1)
    plan = lab.live_plan("stocks", panel, normalize_spec({**MOMENTUM, "hold": 3, "max_names": 6}), (tuesday + 1) * DAY,
                         lab.lab_config(None))
    monday = (dt.date(2026, 11, 30) - dt.date(1970, 1, 1)).days
    assert plan["next_rebalance_day"] == monday                         # Wed, (Thu closed), Fri, Mon: the day of the exit
    leave = dt.datetime.fromtimestamp(plan["exit_at_ms"] / 1000, lab.NY)
    assert leave.date() == dt.date(2026, 11, 30)

    strategy = {"spec": normalize_spec({**MOMENTUM, "hold": 3, "max_names": 6}), "live": dict(plan)}
    friday = monday - 3
    panel.days = np.arange(friday - panel.shape[0] + 1, friday + 1)
    lab._refresh_live("stocks", panel, strategy, (friday + 1) * DAY, lab.lab_config(None))
    assert strategy["live"]["as_of_day"] == tuesday                     # Friday's close is not yet the ranking day

    old_plan = {k: v for k, v in plan.items() if k != "next_rebalance_day"}   # written before sessions were counted
    old_plan["next_rebalance_ordinal"] = 12345
    strategy = {"spec": strategy["spec"], "live": old_plan}
    lab._refresh_live("stocks", panel, strategy, (friday + 1) * DAY, lab.lab_config(None))
    assert strategy["live"]["next_rebalance_day"] == monday and strategy["live"]["as_of_day"] == tuesday


def test_the_writer_never_replaces_a_registry_it_could_not_read(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert lab.load_registry(path)["strategies"] == {}                  # a reader shows an empty lab
    with pytest.raises(ValueError):
        lab.load_registry(path, strict=True)
    with pytest.raises(ValueError):
        lab.run_once(20_780 * DAY, config={}, refresh=False, markets=["crypto"], ask=verdicts(), path=path)
    assert path.read_text(encoding="utf-8") == "{ this is not json"     # the damaged file is left for the owner to see
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        lab.load_registry(path, strict=True)
    assert lab.load_registry(tmp_path / "absent.json", strict=True)["runs"] == []   # a first run starts empty
    path.write_text("\ufeff" + json.dumps({"strategies": {"a": {}}}), encoding="utf-8")
    assert lab.load_registry(path, strict=True)["strategies"] == {"a": {}}          # a byte-order mark is tolerated


def test_a_day_counts_as_done_only_when_its_data_arrived_whole(monkeypatch):
    panel = momentum_panel()
    monkeypatch.setattr(lab_data, "load_panel", lambda market, now_ms=None: panel if market == "crypto" else None)
    monkeypatch.setattr(lab, "default_space", lambda cap=0, min_hold=1: [normalize_spec({**MOMENTUM, "max_names": cap})])
    monkeypatch.setattr(lab, "paper_results", lambda db_path=None, journal_path=None: {})
    monkeypatch.setattr(lab_data, "refresh_crypto", lambda now_ms=None: {"ok": False, "reason": "only 3 of 150 pairs"})
    monkeypatch.setattr(lab_data, "refresh_stocks", lambda now_ms=None: {"ok": True})
    monkeypatch.setattr(lab_data, "refresh_stock_meta", lambda now_ms=None: {"ok": True})
    monkeypatch.setattr(lab_data, "refresh_funding", lambda now_ms=None: {"ok": True})
    now = (int(panel.days[-1]) + 1) * DAY + HOUR
    registry = lab.run_once(now, config={"strategy_lab": {"occupied_kinds": {}}}, refresh=True, ask=verdicts())
    assert registry["last_run_ok"] is False and "last_run_day" not in registry   # the scheduler will try again
    assert registry["runs"][0]["data_refresh"]["reason"] == "only 3 of 150 pairs"

    monkeypatch.setattr(lab_data, "refresh_crypto", lambda now_ms=None: {"ok": True})
    registry = lab.run_once(now, config={"strategy_lab": {"occupied_kinds": {}}}, refresh=True, ask=verdicts())
    assert registry["last_run_ok"] is True and registry["last_run_day"] == now // DAY


def test_only_one_lab_run_at_a_time(monkeypatch):
    from core.strategy_lab import run as lab_run

    first = lab_run.single_run_lock()
    assert first is not None
    try:
        assert lab_run.single_run_lock() is None                        # a second run is refused at once
        assert lab_run.main(["--no-refresh", "--no-review", "--market", "crypto"]) == 3
    finally:
        import os
        os.close(first)
    again = lab_run.single_run_lock()                                   # released with the holder
    assert again is not None
    import os
    os.close(again)


def test_closed_results_come_from_the_trade_journal_and_open_ones_from_the_database(tmp_path):
    import sqlite3

    db = tmp_path / "app.db"
    connection = sqlite3.connect(db)
    connection.execute("create table kv_store (key text primary key, value text)")
    positions = [{"strategy_type": "LAB_ABC123_LONG", "status": "OPEN"},
                 {"strategy_type": "LAB_ABC123_SHORT", "status": "CLOSED", "net_pnl": 5.0, "pnl_pct_notional": 2.5},
                 {"strategy_type": "SCANNER_AI_SHORT", "status": "CLOSED", "net_pnl": 9.0, "pnl_pct_notional": 9.0}]
    connection.execute("insert into kv_store values ('capital_state', ?)", (json.dumps({"positions": positions}),))
    connection.commit(); connection.close()
    journal = tmp_path / "trade_journal.json"
    journal.write_text(json.dumps([
        {"strategy_type": "LAB_ABC123_SHORT", "net_pnl": 5.0, "pnl_pct": 2.5},
        {"strategy_type": "LAB_ABC123_LONG", "net_pnl": -2.0, "pnl_pct": -1.0},     # fell out of the database's 300 rows
        {"strategy_type": "LAB_ABC123_LONG", "net_pnl": 4.0, "pnl_pct": 2.0},
        {"strategy_type": "XS_MOMENTUM_LONG", "net_pnl": 1.0, "pnl_pct": 1.0}, "junk",
    ]), encoding="utf-8")

    results = lab.paper_results(db, journal)
    assert results == {"abc123": {"open": 1, "closed": 3, "wins": 2, "net_pnl_usd": 7.0, "mean_pct": 1.167,
                                  "win_rate_pct": 66.7, "closed_from": "trade journal"}}
    fallback = lab.paper_results(db, tmp_path / "missing.json")
    assert fallback["abc123"]["closed"] == 1 and fallback["abc123"]["closed_from"].startswith("database")
    assert lab.paper_results(tmp_path / "missing.db", journal) == {}
    journal.write_text("[]", encoding="utf-8")
    only_open = lab.paper_results(db, journal)["abc123"]
    assert only_open["closed"] == 0 and only_open["mean_pct"] is None and "sum_pct" not in only_open


def test_an_exploration_rule_that_passed_the_bar_is_not_described_as_having_failed_it(tmp_path):
    path = write_registry(tmp_path)
    registry = json.loads(path.read_text(encoding="utf-8"))
    registry["strategies"]["def456"]["stats"] = {"passes": True}
    path.write_text(json.dumps(registry), encoding="utf-8")
    signals = baskets(tmp_path, {"now": T0 + HOUR}, path=path).candidates({})
    aapl = next(s for s in signals if s["ticker"] == "AAPL")
    assert "passed the lab's statistical bar, but the research lead did not approve it as a strategy" in aapl["reasons"][3]
    assert "did NOT pass" not in aapl["reasons"][3]


def test_the_ui_is_told_when_a_ranking_was_never_entered(monkeypatch):
    from api import strategy_lab_service

    registry = fresh()
    registry["strategies"]["abc"] = {"id": "abc", "market": "crypto", "status": "paper", "description": "x",
                                     "live": {"long": ["ETHUSDT"], "short": [], "missed_window": True}}
    registry["strategies"]["def"] = {"id": "def", "market": "crypto", "status": "explore", "description": "y",
                                     "live": {"long": [], "short": [], "incomplete_data": True}}
    lab.save_registry(registry)
    monkeypatch.setattr(strategy_lab_service, "_config", lambda: lab.lab_config(None))
    monkeypatch.setattr(strategy_lab_service, "paper_results", lambda: {})
    rows = {row["id"]: row for row in strategy_lab_service.strategy_lab_state()["strategies"]}
    assert "entry window had passed" in rows["abc"]["live"]["not_entered"]
    assert "data was incomplete" in rows["def"]["live"]["not_entered"]


def test_stock_history_keeps_the_price_as_traded_and_ranks_without_the_benchmark():
    friday = (dt.date(2026, 11, 20) - dt.date(1970, 1, 1)).days
    now = int(dt.datetime(2026, 11, 23, 12, 0, tzinfo=lab_data.NY).timestamp() * 1000)
    assert lab_data.traded_closes([10.0, 10.0, 1.0, 1.0], [0, 0, 0.1, 0]) == [1.0, 1.0, 1.0, 1.0]     # 1-for-10 reverse split
    assert lab_data.traded_closes([25.0, 100.0], [0, 4.0]) == [100.0, 100.0]                          # 4-for-1 split
    assert lab_data.traded_closes([5.0, 5.0], []) == [5.0, 5.0]

    def download(symbols, period):
        out = {}
        for s in symbols:
            rows = []
            for d in range(friday - 400, friday + 1):
                restated = 3000.0 if s == "PENNY" else 50.0
                traded = 0.5 if s == "PENNY" else 50.0                   # PENNY really traded for cents
                rows.append((d, restated, 60e6, traded))
            out[s] = rows
        return out

    symbols = ["PENNY", "SPY"] + [f"N{i:02d}" for i in range(24)]
    status = lab_data.refresh_stocks(add=symbols, download=download, now_ms=now, max_new=100, sleep=lambda s: None)
    assert status["ok"] and status["without_traded_close"] == 0
    traded = lab_data.load_table(lab_data.lab_dir() / lab_data.TRADED_CLOSE_FILE)
    assert traded["PENNY"][friday][0] == 0.5
    meta = {s: {"rankable": True} for s in symbols if s != "SPY"}
    (lab_data.lab_dir() / lab_data.STOCK_META_FILE).write_text(json.dumps({"fetched_ms": now, "symbols": meta}), encoding="utf-8")
    panel = lab_data.load_panel("stocks", now)
    penny, spy = panel.symbols.index("PENNY") if "PENNY" in panel.symbols else -1, panel.symbols.index("SPY")
    assert penny == -1 or not panel.eligible[:, penny].any()            # a penny stock is never rankable, whatever its restated price
    assert not panel.eligible[:, spy].any() and panel.bench == spy      # the index ETF is the benchmark, not a member
    assert lab_data.refresh_stocks(download=download, now_ms=now, full=True, sleep=lambda s: None)["updated"] == 0   # nothing left to backfill


def test_a_stock_that_leaves_the_directory_keeps_its_history():
    now = 20_780 * DAY
    lab_data.refresh_stock_meta(lambda url: (200, DIRECTORY), now)
    without_aapl = "\n".join(line for line in DIRECTORY.splitlines() if "|AAPL|" not in line) + "\n"
    lab_data.refresh_stock_meta(lambda url: (200, without_aapl), now + 8 * DAY)
    meta = lab_data.load_stock_meta()
    assert meta["AAPL"]["rankable"] is True and meta["AAPL"]["listed"] is False
    rows = {1: (10.0, 1e6)}
    assert "AAPL" in lab_data.rankable_stocks({"AAPL": rows, "SPY": rows}, meta, "SPY")


def test_a_damaged_cache_and_a_partial_crypto_answer_do_not_poison_the_history(tmp_path):
    damaged = tmp_path / "broken.npz"
    damaged.write_bytes(b"PK\x03\x04 this is not a real archive")
    assert lab_data.load_table(damaged) == {}                            # unreadable: start again, do not crash

    api = FakeBinance(now_day=20_780)
    lab_data.refresh_crypto(api, now_ms=20_780 * DAY + HOUR)
    before = lab_data.load_table(lab_data.lab_dir() / "crypto_daily.npz")

    def flaky(url):
        if "klines" in url and "C00USDT" not in url and "C01USDT" not in url:
            return 0, "URLError: timed out"
        return api(url)

    status = lab_data.refresh_crypto(flaky, now_ms=20_781 * DAY + HOUR)
    assert status["ok"] is False and "history left as it was" in status["reason"]
    assert lab_data.load_table(lab_data.lab_dir() / "crypto_daily.npz") == before


def test_the_chart_is_told_where_a_stock_is_really_listed():
    """Seen live: the chart asked TradingView for NASDAQ:DV, which does not exist (DV is on the NYSE)."""
    meta = lab_data.parse_symbol_directory(DIRECTORY)
    assert meta["AAPL"]["exchange"] == "NASDAQ" and meta["BRK-B"]["exchange"] == "NYSE"
    assert meta["SOXL"]["exchange"] == "AMEX"                           # NYSE Arca is AMEX on TradingView
    lab_data.refresh_stock_meta(lambda url: (200, DIRECTORY), 20_780 * DAY)
    assert lab_data.stock_listing("brk.b") == {"ticker": "BRK-B", "known": True, "exchange": "NYSE",
                                               "name": "Berkshire Hathaway Inc. New Common Stock",
                                               "tradingview": "NYSE:BRK.B"}
    assert lab_data.stock_listing("AAPL")["tradingview"] == "NASDAQ:AAPL"
    unknown = lab_data.stock_listing("NOSUCH")
    assert unknown["known"] is False and unknown["tradingview"] == ""  # the chart then lets TradingView search
    assert lab_data.stock_listing(None)["tradingview"] == "" and lab_data.stock_listing("")["known"] is False
