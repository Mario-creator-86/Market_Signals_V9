import json
from collections import Counter

with open("pe_live_8003_fresh.json", encoding="utf-8") as f:
    d = json.load(f)
items = d["items"]

def dir_label(i):
    lab = str(i.get("direction_label") or "").upper()
    if lab in ("LONG","SHORT","NEUTRAL"): return lab
    s = i.get("direction_bias_score") or 0
    return "LONG" if s >= 15 else ("SHORT" if s <= -15 else "NEUTRAL")

def is_already(i): return "ALREADY_REPORTED" in (i.get("badges") or [])
def is_low_cov(i): return (i.get("coverage_score") or 0) < 45 or "LOW_COVERAGE" in (i.get("badges") or [])

active = [i for i in items if not is_already(i)]
prior = [i for i in active if not is_low_cov(i)]
lowc = [i for i in active if is_low_cov(i)]
gap_tbl = [i for i in prior if dir_label(i) == "NEUTRAL"]
long_tbl = [i for i in prior if dir_label(i) == "LONG"]
short_tbl = [i for i in prior if dir_label(i) == "SHORT"]
print("DEFAULT VIEW (range=30, all filters ALL):")
print(f"  HIGH GAP RISK OPPORTUNITIES (NEUTRAL only): {len(gap_tbl)}")
print(f"  LONG BIAS SIGNALS: {len(long_tbl)}")
print(f"  SHORT BIAS SIGNALS: {len(short_tbl)}")
print(f"  NEEDS COVERAGE: {len(lowc)}")
print(f"  ALREADY_REPORTED (excluded): {len([i for i in items if is_already(i)])}")

# HIGH GAP table: how many have NO gap number?
ng = [i for i in gap_tbl if i.get("gap_risk") is None]
print(f"\n  HIGH GAP RISK table rows with gap_risk=None: {len(ng)} of {len(gap_tbl)}")
for i in ng[:10]: print(f"    {i['symbol']:6} gap=None level={i.get('gap_risk_level')} dir={dir_label(i)} score={i.get('direction_bias_score')} cov={i.get('coverage_score')}")
# and their gap distribution
print("  gap values in HIGH GAP table:", sorted([i["gap_risk"] for i in gap_tbl if i.get("gap_risk") is not None], reverse=True)[:15])

# LONG/SHORT tables: gap distribution — how many NONE?
for name, tbl in (("LONG", long_tbl), ("SHORT", short_tbl)):
    none_n = sum(1 for i in tbl if i.get("gap_risk") is None)
    hi = sum(1 for i in tbl if (i.get("gap_risk") or 0) >= 70)
    print(f"  {name} table: n={len(tbl)}, gap NONE={none_n}, gap>=70={hi}")

# INSUFFICIENT_DATA direction items — where do they land?
ins = [i for i in items if str(i.get("direction_label") or "").upper() == "INSUFFICIENT_DATA"]
print(f"\nINSUFFICIENT_DATA items: {len(ins)}")
for i in ins[:12]:
    print(f"    {i['symbol']:6} score={i.get('direction_bias_score')} ui_label={dir_label(i)} cov={i.get('coverage_score')} gap={i.get('gap_risk')}")

# ALREADY_REPORTED items detail
print("\nALREADY_REPORTED items:")
for i in items:
    if is_already(i):
        fl = i.get("flags") or {}
        print(f"    {i['symbol']:6} event={i.get('earnings_time_utc')} dir={i.get('direction_label')} gap={i.get('gap_risk')} ev={str(fl.get('already_reported_evidence'))[:90]}")

# badges distribution
bcount = Counter()
for i in items:
    for b in (i.get("badges") or []): bcount[b] += 1
print("\nbadges:", dict(bcount))

# expected move fields comparison
print("\nexpected move fields (sample high-gap):")
for i in items[:6]:
    print(f"    {i['symbol']:6} expected_move_pct={i.get('expected_move_pct')} range_proxy_pct={i.get('range_proxy_pct')} hist_median={i.get('historical_median_move_pct')} avg_hist={i.get('avg_move_historical')}")

# avg confidence stat
confs = [i.get("confidence_score") or 0 for i in items]
print("\nAvg confidence shown in header:", round(sum(confs)/len(confs)), "%")
hi70 = sum(1 for i in items if (i.get("gap_risk") or 0) >= 70)
print("Header stats: total", len(items), "| high risk(>=70)", hi70, "| bullish(LONG)", sum(1 for i in items if dir_label(i)=="LONG"))

# watchlist markers?
wl = [i for i in items if i.get("is_watchlist")]
print("items with is_watchlist flag:", len(wl))
