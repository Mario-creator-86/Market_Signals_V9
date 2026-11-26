import json
with open("pe_live_8003_fresh.json", encoding="utf-8") as f:
    d = json.load(f)
items = d["items"]
def is_low_cov(i): return (i.get("coverage_score") or 0) < 45 or "LOW_COVERAGE" in (i.get("badges") or [])
def is_ar(i): return "ALREADY_REPORTED" in (i.get("badges") or [])
# default view LONG/SHORT tables with gap NONE (prioritized!)
for i in items:
    lab = str(i.get("direction_label") or "").upper()
    if lab in ("LONG","SHORT") and i.get("gap_risk") is None and not is_low_cov(i) and not is_ar(i):
        print(f"DEFAULT-VIEW {lab} table, gap N/A: {i['symbol']} score={i.get('direction_bias_score')} cov={i.get('coverage_score')} conf={i.get('confidence_score')} ai={(i.get('ai_decision') or {}).get('verdict')}")
# XMTR check
x = next(i for i in items if i["symbol"]=="XMTR")
print("\nXMTR:", x.get("direction_label"), x.get("direction_bias_score"), "gap=", x.get("gap_risk"), "cov=", x.get("coverage_score"), "badges=", x.get("badges"))
# earnings cache age hours
print("\nearnings cache age hours:", round(d["status"]["last_inputs_used"]["earnings_cache_age_seconds"]/3600,1))
