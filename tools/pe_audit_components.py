import json
from collections import Counter
with open("pe_live_8003_fresh.json", encoding="utf-8") as f:
    d = json.load(f)
items = d["items"]
by = {i["symbol"]: i for i in items}

# LONG/SHORT with gap NONE — what are components made of?
nogap_dir = [i for i in items if i.get("gap_risk") is None and i.get("direction_label") in ("LONG","SHORT")]
hist_vals = Counter()
for i in nogap_dir:
    c = i.get("components") or {}
    hist_vals[str(c.get("historical"))] += 1
print("gap-NONE directional items:", len(nogap_dir))
print("  components.historical values:", dict(hist_vals))

for s in ("RDVT","INSW","VECO","SOPH","WULF"):
    i = by[s]
    c = i.get("components") or {}
    print(f"\n{s}: label={i.get('direction_label')} score={i.get('direction_bias_score')} structured={i.get('structured_direction_bias_score')}")
    print(f"  components={json.dumps(c)}")
    print(f"  beat_rate_historical={i.get('beat_rate_historical')} beats={i.get('beat_rate_beats')} samples={i.get('beat_rate_samples')} unavail={str(i.get('beat_rate_unavailable_reason'))[:60]}")
    print(f"  why0={str((i.get('why') or [''])[0])[:90]}")

# how many gap-NONE items have historical == 0.0 vs None vs 100
c = Counter()
for i in items:
    if i.get("gap_risk") is None:
        c[str((i.get("components") or {}).get("historical"))] += 1
print("\nALL gap-NONE: components.historical distribution:", dict(c))

# phantom GapRisk= in why0 for gap-NONE items
w = Counter()
for i in items:
    if i.get("gap_risk") is None:
        w[str((i.get("why") or [""])[0])[:60]] += 1
print("\nwhy[0] for gap-NONE items:")
for k,v in w.most_common(4): print(f"  [{v}] {k}")

# options_implied_move vs range_proxy_pct equality
both = [(i["symbol"], (i.get("components") or {}).get("options_implied_move"), i.get("range_proxy_pct"), i.get("expected_move_pct")) for i in items if (i.get("components") or {}).get("options_implied_move") is not None]
print("\nitems with options_implied_move:", len(both))
eq = sum(1 for s,o,r,e in both if o == r)
print("  where options_implied_move == range_proxy_pct:", eq)
for s,o,r,e in both[:6]: print(f"    {s}: implied={o} range={r} expected={e}")

# does implied move feed gap_risk for those 75? check gap contributors field
i0 = next(i for i in items if (i.get("components") or {}).get("options_implied_move") is not None)
print("\nsample contributors keys:", list((i0.get("contributors") or {}).keys())[:20])
print("sample contributors:", json.dumps(i0.get("contributors"))[:400])
