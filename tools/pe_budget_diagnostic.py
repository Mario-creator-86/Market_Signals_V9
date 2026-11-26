"""Empirical check: why do 170/200 symbols have no GapRisk?

Runs the agent's own data path for a few 'NONE' symbols OUTSIDE any budget
context. If data appears, the production failure is budget starvation; if not,
the provider path itself is broken.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.pre_earnings_agent import PreEarningsAgent  # noqa: E402

agent = PreEarningsAgent.__new__(PreEarningsAgent)
# minimal attrs used by the path under test
agent._daily_bars_cache = {}
agent._yf_earnings_history_cache = {}

data = json.load(open("data/pre_earnings.json", encoding="utf-8"))
none_syms = [it["symbol"] for it in data["items"] if str(it.get("gap_risk_level")) == "NONE"][:4]
ok_syms = [it["symbol"] for it in data["items"] if str(it.get("gap_risk_level")) != "NONE"][:2]

for sym in none_syms + ok_syms:
    bars = agent._load_symbol_daily_bars(sym)
    dates = agent._resolve_yf_past_earnings_dates(sym, n=4)
    stats = agent._compute_historical_earnings_gap_stats(sym)
    print(f"{sym:6s} bars={len(bars):3d} past_dates={len(dates)} "
          f"sample={stats['sample_size']} median={stats['median_move_pct']} src={stats['source']}")
