# Read-only audit of the live pre-earnings payload
import json, sys
from datetime import datetime, timezone
from collections import Counter, defaultdict

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

d = load("pe_live_8003_fresh.json")
items = d.get("items", [])
now = datetime.now(timezone.utc)
print("=== TOP-LEVEL ===")
print("as_of_utc:", d.get("as_of_utc"))
as_of = d.get("as_of_utc")
if as_of:
    try:
        a = datetime.fromisoformat(as_of.replace("Z","+00:00"))
        print("as_of age (minutes):", round((now-a).total_seconds()/60,1))
    except Exception as e: print("as_of parse err", e)
print("status:", json.dumps(d.get("status", {}), indent=1)[:800])
print("n items:", len(items))
print("lookahead_days:", d.get("lookahead_days"), "refresh_seconds:", d.get("refresh_seconds"))
print("flags:", d.get("flags"))
print("reported_today count:", len(d.get("reported_today") or []))
for r in (d.get("reported_today") or [])[:15]:
    print("  reported_today:", r)

print("\n=== FIELD DISTRIBUTIONS ===")
print("gap_risk_level:", Counter(i.get("gap_risk_level") for i in items))
print("direction_label:", Counter(i.get("direction_label") for i in items))
print("structured_direction_label:", Counter(i.get("structured_direction_label") for i in items))
print("session:", Counter(i.get("session") for i in items))
print("confirmed:", Counter(i.get("confirmed") for i in items))
print("data_confidence:", Counter(i.get("data_confidence") for i in items))
print("coverage_label:", Counter(i.get("coverage_label") for i in items))

# 1. direction_label vs score consistency
print("\n=== LABEL vs SCORE MISMATCHES ===")
mism = []
for i in items:
    s = i.get("direction_bias_score"); lab = i.get("direction_label")
    if s is None or lab in (None, "INSUFFICIENT_DATA"): continue
    expect = "LONG" if s >= 15 else ("SHORT" if s <= -15 else "NEUTRAL")
    if lab != expect:
        mism.append((i["symbol"], lab, s, expect))
print("mismatches:", len(mism))
for m in mism[:20]: print("  ", m)

# 2. LONG/SHORT with gap_risk NONE
print("\n=== DIRECTIONAL CALLS WITH NO GAP ESTIMATE ===")
nogap = [i for i in items if (i.get("gap_risk_level") in (None,"NONE") or i.get("gap_risk_unavailable")) and i.get("direction_label") in ("LONG","SHORT")]
print("count:", len(nogap))
for i in nogap[:25]:
    print(f"  {i['symbol']:6} {i['direction_label']:5} score={i.get('direction_bias_score')} gap={i.get('gap_risk')} level={i.get('gap_risk_level')} unavail={i.get('gap_risk_unavailable')} reason={str(i.get('gap_risk_unavailable_reason'))[:60]} conf={i.get('confidence_score')} cov={i.get('coverage_score')}")

# 3. Session-aware date analysis
print("\n=== EVENT TIME ANALYSIS (now = %s) ===" % now.isoformat())
past, today_bmo_window, today_amc_window, future, nodate, unpar = [], [], [], [], [], []
for i in items:
    t = i.get("earnings_time_utc"); sess = i.get("session")
    if not t:
        nodate.append(i); continue
    try:
        et = datetime.fromisoformat(t.replace("Z","+00:00"))
    except Exception:
        unpar.append((i["symbol"], t)); continue
    if et < now - __import__("datetime").timedelta(hours=24):
        past.append((i["symbol"], t, sess))
    elif et < now:
        # in last 24h — check session-aware rule
        past.append((i["symbol"], t, sess))
    else:
        future.append((i["symbol"], t, sess))
print("past (et < now):", len(past))
for p in sorted(past, key=lambda x: x[1])[:30]: print("  PAST:", p)
print("future:", len(future), "no date:", len(nodate), "unparseable:", len(unpar))
for n in nodate[:10]: print("  NODATE:", n["symbol"], n.get("direction_label"), n.get("gap_risk"))

# 4. Confidence / coverage vs conviction
print("\n=== HIGH CONVICTION / LOW DATA ===")
weird = [i for i in items if i.get("direction_label") in ("LONG","SHORT") and (i.get("confidence_score") or 0) < 40]
print("LONG/SHORT with confidence_score<40:", len(weird))
for i in weird[:15]:
    print(f"  {i['symbol']:6} {i['direction_label']:5} score={i.get('direction_bias_score')} conf={i.get('confidence_score')} cov={i.get('coverage_score')} data_conf={i.get('data_confidence')} msg={str(i.get('data_message'))[:60]}")

# 5. AI verdict vs final label contradiction
print("\n=== AI VERDICT vs FINAL LABEL ===")
contra = []
for i in items:
    ai = i.get("ai_decision") or {}
    v = ai.get("verdict"); lab = i.get("direction_label")
    if v in ("LONG","SHORT") and lab in ("LONG","SHORT") and v != lab:
        contra.append((i["symbol"], v, ai.get("confidence"), lab, i.get("direction_bias_score"), i.get("structured_direction_bias_score"), i.get("llm_direction_score")))
print("direct contradictions:", len(contra))
for c in contra[:20]: print("  ", c)

# 6. Pulse
print("\n=== PULSE ===")
p = d.get("pulse")
if p:
    print("generated_at:", p.get("generated_at"), "model:", p.get("model"), "sentiment:", p.get("sentiment"))
    print("top_long:", [(x.get("symbol"), x.get("earnings_time_utc") or x.get("event_date"), str(x.get("reason"))[:50]) for x in (p.get("top_long") or [])])
    print("top_short:", [(x.get("symbol"), x.get("earnings_time_utc") or x.get("event_date"), str(x.get("reason"))[:50]) for x in (p.get("top_short") or [])])
    syms_in_items = {i["symbol"] for i in items}
    for side in ("top_long","top_short"):
        for x in (p.get(side) or []):
            s = x.get("symbol")
            if s not in syms_in_items:
                print(f"  !! pulse pick {s} NOT in items")
            else:
                it = next(i for i in items if i["symbol"]==s)
                print(f"  pulse {side} {s}: item label={it.get('direction_label')} score={it.get('direction_bias_score')} gap={it.get('gap_risk')} level={it.get('gap_risk_level')} event={it.get('earnings_time_utc')} already_rep={(it.get('flags') or {}).get('already_reported')}")
else:
    print("pulse is None")

# 7. stale / duplicated events
print("\n=== DUPLICATE SYMBOLS ===")
c = Counter(i["symbol"] for i in items)
dups = {k:v for k,v in c.items() if v>1}
print(dups)

# 8. ai_decision usage
print("\n=== AI DECISION ===")
used = sum(1 for i in items if (i.get("ai_decision") or {}).get("used"))
print("ai_decision.used:", used, "/", len(items))

# 9. opportunity/coverage score sanity
print("\n=== SCORE RANGES ===")
def rng(key):
    vals = [i.get(key) for i in items if isinstance(i.get(key),(int,float))]
    if vals: print(f"{key}: min={min(vals)} max={max(vals)} n={len(vals)}")
for k in ("gap_risk_numeric","direction_bias_score","structured_direction_bias_score","llm_direction_score","confidence_score","coverage_score","opportunity_score","expected_move_pct","historical_median_move_pct"):
    rng(k)

# 10. expected move present?
print("\n=== EXPECTED MOVE / HISTORY PRESENCE ===")
em = sum(1 for i in items if i.get("expected_move_pct") is not None)
hm = sum(1 for i in items if i.get("historical_median_move_pct") is not None)
rr = sum(1 for i in items if i.get("recent_reactions"))
tr = sum(1 for i in items if i.get("symbol_track_record"))
print(f"expected_move_pct present: {em}/{len(items)}; hist median move: {hm}; recent_reactions: {rr}; symbol_track_record: {tr}")

# 11. decision_alignment field
print("\n=== decision_alignment ===")
print(Counter(str(i.get("decision_alignment")) for i in items))

# 12. flags per item
flagcount = Counter()
for i in items:
    f = i.get("flags") or {}
    for k,v in f.items():
        if v: flagcount[k]+=1
print("item flags:", dict(flagcount))
