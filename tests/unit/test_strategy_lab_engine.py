"""Strategy Lab engine: a rule is data, and the backtest must be honest.

The tests that matter most here are the ones about what the backtest is NOT
allowed to know: tomorrow's prices, tomorrow's volume, a coin's first weeks.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.strategy_lab.engine import (
    Panel, daily_returns, default_space, describe_spec, distinct, evaluate, gate_matrix, members_at,
    normalize_spec, passes, rank_key, run_search, signal_matrix, spec_id,
)

SPEC = {"family": "xs_rank", "metric": "return", "lookback": 5, "skip": 0, "direction": "follow",
        "fraction": 2, "hold": 3, "sides": "both", "gate": {"kind": "none"}}


def spec(**changes):
    return normalize_spec({**SPEC, **changes})


def random_panel(days=160, symbols=12, seed=7, **kwargs):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0, 0.03, (days, symbols)), axis=0)
    volume = np.full((days, symbols), 50e6)
    names = [f"S{j:02d}" for j in range(symbols)]
    return Panel("crypto", np.arange(20_000, 20_000 + days), names, close, volume, "S00", 0.001, 10e6, **kwargs)


def steady_panel(rates, days=60, cost=0.0, **kwargs):
    """Every instrument grows by its own fixed daily rate: the ranking never changes."""
    close = np.array([[100 * (1 + r) ** d for r in rates] for d in range(days)])
    volume = np.full((days, len(rates)), 50e6)
    return Panel("crypto", np.arange(20_000, 20_000 + days), [f"S{j}" for j in range(len(rates))],
                 close, volume, "S0", cost, 10e6, **kwargs)


# ------------------------------------------------------------------ specs
def test_a_spec_is_validated_and_gets_a_stable_id():
    good = spec()
    assert good["max_names"] == 0 and good["gate"] == {"kind": "none"}
    assert spec_id(good) == spec_id(normalize_spec(dict(reversed(list(good.items())))))
    assert spec_id(good) != spec_id(spec(lookback=6))
    assert "long the highest 1/2, short the lowest 1/2" in describe_spec(good)
    assert "at most 4 names a side" in describe_spec(spec(max_names=4))


@pytest.mark.parametrize("bad", [
    {"metric": "magic"}, {"direction": "sideways"}, {"sides": "none"}, {"lookback": 1}, {"lookback": 900},
    {"skip": 5, "lookback": 5}, {"fraction": 1}, {"hold": 0}, {"hold": 99}, {"gate": {"kind": "moon"}},
    {"family": "python_code"}, {"lookback": "abc"}, {"max_names": 500},
])
def test_anything_else_is_not_a_rule(bad):
    assert normalize_spec({**SPEC, **bad}) is None
    assert normalize_spec("rm -rf") is None and normalize_spec(None) is None


def test_the_default_space_is_valid_and_has_no_duplicates():
    space = default_space(max_names=5)
    ids = [spec_id(s) for s in space]
    assert len(space) > 300 and len(ids) == len(set(ids))
    assert all(s["max_names"] == 5 and s["hold"] <= s["lookback"] for s in space)
    slow = default_space(max_names=6, min_hold=3)                      # stocks: no one-day rules
    assert slow and all(s["hold"] >= 3 for s in slow) and len(slow) < len(space)


# ------------------------------------------------------- no knowledge of the future
@pytest.mark.parametrize("changes", [{}, {"metric": "volatility"}, {"metric": "high_proximity"},
                                     {"metric": "volume_trend"}, {"skip": 1}, {"gate": {"kind": "bench_trend_sides", "n": 10}}])
def test_a_signal_never_depends_on_later_prices_or_volume(changes):
    rule = spec(**changes)
    base = random_panel()
    cut = 100
    close, volume = base.close.copy(), base.dollar_volume.copy()
    rng = np.random.default_rng(99)
    close[cut + 1:] *= rng.uniform(0.5, 2.0, close[cut + 1:].shape)      # a different future
    volume[cut + 1:] *= rng.uniform(0.0, 3.0, volume[cut + 1:].shape)
    other = Panel("crypto", base.days, base.symbols, close, volume, "S00", 0.001, 10e6)

    for offset in range(rule["hold"]):
        assert np.array_equal(signal_matrix(base, rule, offset)[:cut + 1], signal_matrix(other, rule, offset)[:cut + 1])
        net_a, live_a = daily_returns(base, signal_matrix(base, rule, offset), rule["hold"], offset)
        net_b, live_b = daily_returns(other, signal_matrix(other, rule, offset), rule["hold"], offset)
        # Everything up to the cut is the same, except the closing cost of a period that ends later.
        last_ranking = max(i for i in range(cut + 1) if i % rule["hold"] == offset % rule["hold"])
        assert np.allclose(net_a[:last_ranking + 1], net_b[:last_ranking + 1])
        assert np.array_equal(live_a[:cut + 1], live_b[:cut + 1])


def test_the_position_starts_the_day_after_the_signal():
    panel = steady_panel([0.03, 0.02, 0.01, -0.01, -0.02, -0.03])
    sig = signal_matrix(panel, spec(hold=1))
    first = int(np.flatnonzero(np.abs(sig).sum(axis=1))[0])
    net, live = daily_returns(panel, sig, 1, 0)
    assert not live[first] and live[first + 1]                         # nothing is earned on the signal day itself
    assert net[first] == 0.0


def test_where_the_market_closes_overnight_the_entry_is_one_close_later():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03]
    around_the_clock = steady_panel(rates)
    overnight = steady_panel(rates, entry_lag=1)
    rule = spec(hold=5)
    sig = signal_matrix(around_the_clock, rule)
    first = int(np.flatnonzero(np.abs(sig).sum(axis=1))[0])
    _, live_now = daily_returns(around_the_clock, sig, 5, 0)
    _, live_later = daily_returns(overnight, sig, 5, 0)
    assert live_now[first + 1] and not live_later[first + 1] and live_later[first + 2]   # the gap after the ranking is not earned
    _, never = daily_returns(overnight, signal_matrix(overnight, spec(hold=1)), 1, 0)
    assert not never.any()                                             # a one-day rule cannot be traded there at all


def test_a_short_that_keeps_rising_costs_its_full_compounded_move():
    days = 12
    close = np.ones((days, 6)) * 100.0
    close[5:, 5] = 100.0 * 1.5 ** np.arange(1, days - 4)               # S5 rises 50% a day from day 5
    panel = Panel("crypto", np.arange(20_000, 20_000 + days), [f"S{j}" for j in range(6)], close,
                  np.full((days, 6), 50e6), "S0", 0.0, 10e6)
    sig = np.zeros((days, 6)); sig[4, 5] = -1.0                        # shorted at the close of day 4, held 3 days
    net, live = daily_returns(panel, sig, hold=3, offset=1)
    held = net[5:8]
    assert held.sum() == pytest.approx(-(1.5 ** 3 - 1))                # -237.5%: more than the amount put in
    assert held[2] < held[1] < held[0] < 0                             # each day costs more than the day before


def test_a_fresh_listing_cannot_be_picked_until_it_is_seasoned():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03, 0.0]
    panel = steady_panel(rates, days=80)
    close = panel.close.copy()
    close[:30, 6] = np.nan                                             # listed on day 30 ...
    close[30:, 6] = 100 * np.cumprod(np.full(50, 1.5))                 # ... and it explodes
    volume = panel.dollar_volume.copy()
    rule = spec(hold=1, fraction=2)
    unseasoned = Panel("crypto", panel.days, panel.symbols, close, volume, "S0", 0.0, 10e6)
    seasoned = Panel("crypto", panel.days, panel.symbols, close, volume, "S0", 0.0, 10e6, min_history_days=30)

    assert signal_matrix(unseasoned, rule)[45, 6] == 1.0               # without the rule it is bought at once
    picked = np.flatnonzero(signal_matrix(seasoned, rule)[:, 6])
    assert picked.min() >= 30 + 30 - 1                                 # with it, only a month after listing


def test_an_instrument_is_liquid_only_on_past_volume():
    panel = steady_panel([0.03, 0.02, 0.01, -0.01, -0.02, -0.03], days=40)
    volume = panel.dollar_volume.copy()
    volume[:20, 0] = 1e6                                               # S0 is thin until day 20
    thin = Panel("crypto", panel.days, panel.symbols, panel.close, volume, "S1", 0.0, 10e6)
    assert not thin.liquid[20, 0]                                      # day 20 itself does not count yet
    assert not thin.liquid[21, 0]                                      # days 14-20: (6 x $1M + $50M) / 7 = $8M
    assert thin.liquid[22, 0]                                          # days 15-21 average $15M: liquid from here


# ------------------------------------------------------------- the arithmetic
def test_returns_costs_and_both_legs_add_up():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03]
    panel = steady_panel(rates, cost=0.001)
    net, live = daily_returns(panel, signal_matrix(panel, spec(hold=1)), 1, 0)
    days = np.flatnonzero(live)
    gross = (0.03 + 0.02 + 0.01 + 0.01 + 0.02 + 0.03) / 6               # three longs, three shorts, equal amounts
    assert net[days[0]] == pytest.approx(gross - 2 * 0.001)            # in and out every day: two costs a day
    held_five = daily_returns(panel, signal_matrix(panel, spec(hold=5)), 5, 0)[0]
    block = np.flatnonzero(held_five)[:5]
    grown = [((1 + r) ** 5 - 1) for r in rates[:3]] + [(1 - (1 + r) ** 5) for r in rates[3:]]
    assert held_five[block].sum() == pytest.approx(sum(grown) / 6 - 2 * 0.001)   # one entry, one exit in five days


def test_direction_sides_and_the_name_cap():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03]
    panel = steady_panel(rates)
    row = panel.shape[0] - 1
    assert members_at(panel, spec(), row) == ([0, 1, 2], [5, 4, 3])    # most extreme first
    assert members_at(panel, spec(direction="against"), row) == ([5, 4, 3], [0, 1, 2])
    assert members_at(panel, spec(sides="long"), row) == ([0, 1, 2], [])
    assert members_at(panel, spec(sides="short"), row) == ([], [5, 4, 3])
    assert members_at(panel, spec(max_names=1), row) == ([0], [5])
    capped = signal_matrix(panel, spec(max_names=1))
    assert np.abs(capped[row]).sum() == 2                              # the backtest holds what the live list holds


def test_too_few_instruments_means_no_position():
    panel = steady_panel([0.02, 0.01, -0.01, -0.02])                   # 4 names, fraction 2 needs 6
    assert members_at(panel, spec(), panel.shape[0] - 1) == ([], [])
    assert not signal_matrix(panel, spec()).any()


def test_the_benchmark_gate_switches_sides():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03]                    # benchmark S0 rises all the time
    panel = steady_panel(rates)
    row = panel.shape[0] - 1
    both = spec(gate={"kind": "bench_trend_sides", "n": 10})
    assert members_at(panel, both, row) == ([0, 1, 2], [])             # above its average: longs only
    assert members_at(panel, spec(gate={"kind": "bench_below_sma", "n": 10}), row) == ([], [])
    allow_long, allow_short = gate_matrix(panel, both)
    assert not allow_long[5] and not allow_short[5]                    # the average is not known yet: no trade


def test_funding_is_paid_by_longs_and_received_by_shorts():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03]
    flat = steady_panel(rates)
    carry = np.zeros(flat.shape); carry[:, :3] = 0.001; carry[:, 3:] = -0.002   # longs pay 0.1%, shorts pay 0.2% a day
    funded = steady_panel(rates, carry=carry)
    rule = spec(hold=1)
    plain, live = daily_returns(flat, signal_matrix(flat, rule), 1, 0)
    paid, _ = daily_returns(funded, signal_matrix(funded, rule), 1, 0)
    day = int(np.flatnonzero(live)[3])
    assert plain[day] - paid[day] == pytest.approx((3 * 0.001 + 3 * 0.002) / 6)


def test_a_stock_short_pays_a_borrow_fee_and_a_long_does_not():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03]
    free = steady_panel(rates)
    borrowed = steady_panel(rates, borrow_daily=0.001)
    for sides, names_short in (("both", 3), ("short", 3), ("long", 0)):
        rule = spec(hold=1, sides=sides)
        plain, live = daily_returns(free, signal_matrix(free, rule), 1, 0)
        paid, _ = daily_returns(borrowed, signal_matrix(borrowed, rule), 1, 0)
        day = int(np.flatnonzero(live)[3])
        held = 6 if sides == "both" else 3
        assert plain[day] - paid[day] == pytest.approx(names_short * 0.001 / held)


# ------------------------------------------------------------------ the bar
def test_a_rule_is_judged_on_train_holdout_and_the_whole_history():
    rng = np.random.default_rng(3)
    days, symbols = 420, 20
    drift = np.linspace(-0.004, 0.004, symbols)                        # persistent winners and losers
    close = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, (days, symbols)), axis=0)
    panel = Panel("crypto", np.arange(20_000, 20_000 + days), [f"S{j:02d}" for j in range(symbols)], close,
                  np.full((days, symbols), 50e6), "S10", 0.0005, 10e6)
    good = evaluate(panel, spec(lookback=21, hold=7, fraction=5))
    bad = evaluate(panel, spec(lookback=21, hold=7, fraction=5, direction="against"))

    assert good["passes"] and not bad["passes"]
    assert good["train"]["days"] + good["holdout"]["days"] == good["full"]["days"]
    assert abs(good["full"]["beta"]) < 0.5                             # two legs: little market exposure
    assert rank_key(good) > rank_key(bad)
    results = run_search(panel, [spec(lookback=21, hold=7, fraction=5), spec(lookback=21, hold=7, fraction=5),
                                 spec(lookback=14, hold=7, fraction=5), {"family": "nonsense"}])
    assert len(results) == 2 and results[0]["id"] in {r["id"] for r in results}
    assert len(distinct(results, 5)) == 1                              # same kind of rule: one representative


def test_one_sided_rules_are_not_rewarded_for_the_market_direction():
    rng = np.random.default_rng(5)
    days, symbols = 420, 20
    close = 100 * np.cumprod(1 - 0.004 + rng.normal(0, 0.01, (days, symbols)), axis=0)   # everything falls
    panel = Panel("crypto", np.arange(20_000, 20_000 + days), [f"S{j:02d}" for j in range(symbols)], close,
                  np.full((days, symbols), 50e6), "S10", 0.0005, 10e6)
    short_only = evaluate(panel, spec(lookback=21, hold=7, fraction=5, sides="short"))

    assert short_only["full"]["total_pct"] > 50                        # it made money ...
    assert not short_only["passes"]                                    # ... but only because the market fell
    assert abs(short_only["full"]["alpha_t"]) < 2.0


def test_the_bar_needs_every_part():
    part = {"ok": True, "sharpe": 2.0, "alpha_t": 3.0, "mean_daily_pct": 0.1, "quarters_positive": 5, "quarters": 6,
            "max_drawdown_pct": -10.0}
    assert passes({"train": part, "holdout": part, "full": part})
    assert not passes({"train": part, "holdout": {**part, "alpha_t": 0.2}, "full": part})
    assert not passes({"train": part, "holdout": {**part, "mean_daily_pct": -0.01}, "full": part})
    assert not passes({"train": {**part, "sharpe": 0.5}, "holdout": part, "full": part})
    assert not passes({"train": part, "holdout": part, "full": {**part, "max_drawdown_pct": -60.0}})
    assert not passes({"train": part, "holdout": part, "full": {**part, "quarters_positive": 2}})
    assert not passes({"train": part, "holdout": {"ok": False}, "full": part})


# ------------------------------------------------ findings of the independent review (2026-11-23)
def test_the_price_floor_uses_the_price_as_it_traded_not_a_price_restated_by_later_splits():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03, -0.05]
    base = steady_panel(rates, days=60)
    close = base.close.copy()
    close[:, 6] = 2500.0 * (1 - 0.05) ** np.arange(60)                # restated: looks like a $2,500 stock ...
    traded = close.copy()
    traded[:, 6] = close[:, 6] / 6000.0                                # ... that really traded for cents
    restated_only = Panel("stocks", base.days, base.symbols, close, base.dollar_volume, "S0", 0.0, 10e6, min_price=5.0)
    with_floor = Panel("stocks", base.days, base.symbols, close, base.dollar_volume, "S0", 0.0, 10e6, min_price=5.0,
                       price_floor=traded)
    row = 59
    assert restated_only.eligible[row, 6] and not with_floor.eligible[row, 6]
    assert 6 in members_at(restated_only, spec(sides="short"), row)[1]  # the artefact: a penny stock shorted "at $2,500"
    assert 6 not in members_at(with_floor, spec(sides="short"), row)[1]
    gaps = traded.copy(); gaps[10, 2] = np.nan                         # a missing as-traded bar falls back to the close
    assert Panel("stocks", base.days, base.symbols, close, base.dollar_volume, "S0", 0.0, 10e6, min_price=5.0,
                 price_floor=gaps).eligible[10, 2]


def test_the_live_row_is_ranked_with_the_same_test_as_every_backtest_row():
    rates = [0.03, 0.02, 0.01, -0.01, -0.02, -0.03, 0.05]
    base = steady_panel(rates, days=40)
    volume = base.dollar_volume.copy()
    volume[:, 6] = 1e6
    volume[-1, 6] = 400e6                                              # S6 becomes liquid on the last day itself
    panel = Panel("crypto", base.days, base.symbols, base.close, volume, "S0", 0.0, 10e6)
    last = panel.shape[0] - 1
    assert panel.eligible[last, 6] and not panel.eligible[last - 1, 6]
    assert 6 in members_at(panel, spec(), last)[0]                     # the live list contains it, as a backtest row would
    longer = Panel("crypto", np.arange(20_000, 20_041), base.symbols, np.vstack([base.close, base.close[-1:] * 1.01]),
                   np.vstack([volume, volume[-1:]]), "S0", 0.0, 10e6)
    assert members_at(longer, spec(), last) == members_at(panel, spec(), last)   # one more day changes nothing about that row


def test_an_index_benchmark_is_never_a_member():
    rates = [0.09, 0.02, 0.01, -0.01, -0.02, -0.03, -0.09]
    ranked = steady_panel(rates)
    unranked = steady_panel(rates, rank_benchmark=False)
    row = ranked.shape[0] - 1
    assert 0 in members_at(ranked, spec(), row)[0]
    assert 0 not in members_at(unranked, spec(), row)[0] and not unranked.eligible[:, 0].any()
    assert unranked.bench == 0                                         # it still is the benchmark of the gates


def test_risk_figures_are_those_of_one_schedule_and_the_edge_is_that_of_all():
    rng = np.random.default_rng(3)
    days, symbols = 420, 20
    drift = np.linspace(-0.004, 0.004, symbols)
    close = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, (days, symbols)), axis=0)
    panel = Panel("crypto", np.arange(20_000, 20_000 + days), [f"S{j:02d}" for j in range(symbols)], close,
                  np.full((days, symbols), 50e6), "S10", 0.0005, 10e6)
    rule = spec(lookback=21, hold=7, fraction=5)
    result = evaluate(panel, rule)
    full = result["full"]
    assert full["schedules"] == 7 and "sharpe_all_schedules" in full
    singles = []
    from core.strategy_lab.engine import market_return, summarize
    for offset in range(7):
        net, live = daily_returns(panel, signal_matrix(panel, rule, offset), 7, offset)
        singles.append(summarize(net, live, market_return(panel), panel.days))
    assert full["max_drawdown_pct"] == min(s["max_drawdown_pct"] for s in singles)   # live lands on an arbitrary schedule
    assert full["sharpe"] == round(float(np.median([s["sharpe"] for s in singles])), 2)
    assert full["sharpe_all_schedules"] >= full["sharpe"]              # the blend of seven books looks smoother than one
    one_day = evaluate(panel, spec(lookback=5, hold=1, fraction=5))
    assert "schedules" not in one_day["full"]                          # a daily rule has a single schedule


def test_days_without_a_position_count_as_days_without_return():
    from core.strategy_lab.engine import summarize

    net = np.zeros(300); live = np.zeros(300, dtype=bool)
    rng = np.random.default_rng(1)
    for start in (10, 110, 210):
        live[start:start + 40] = True
        net[start:start + 40] = 0.002 + rng.normal(0, 0.01, 40)
    days = np.arange(20_000, 20_300)
    stats = summarize(net, live, np.zeros(300), days)
    assert stats["days"] == 240 and stats["days_in_market"] == 120     # from the first held day to the last
    in_market_only = float(net[live].mean() / net[live].std(ddof=1) * np.sqrt(365))
    assert stats["sharpe"] < in_market_only                            # being flat half the time is not free
    part = np.zeros(300, dtype=bool); part[:150] = True
    assert summarize(net, live, np.zeros(300), days, part)["days"] == 140
    assert summarize(net, np.zeros(300, dtype=bool), np.zeros(300), days)["ok"] is False


def test_a_panel_can_be_cut_back_to_how_it_stood_some_bars_ago():
    panel = random_panel(min_history_days=20)
    earlier = panel.truncated(10)
    assert earlier.shape == (150, 12) and int(earlier.days[-1]) == int(panel.days[-11])
    assert np.array_equal(earlier.eligible, panel.eligible[:-10])       # nothing about the past depends on later bars
    assert panel.truncated(0) is panel
