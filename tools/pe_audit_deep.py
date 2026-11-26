import json
with open("pe_live_8003_fresh.json", encoding="utf-8") as f:
    d = json.load(f)
items = d["items"]

# options components availability
n_opt = sum(1 for i in items if (i.get("components") or {}).get("options") is not None or (i.get("direction_components") or {}).get("c3_options") is not None)
print("items with any options component:", n_opt)
# implied move
n_impl = sum(1 for i in items if (i.get("components") or {}).get("options_implied_move") is not None)
print("items with options_implied_move:", n_impl)

# symbol_track_record sample + track_record_overall
tro = next((i.get("track_record_overall") for i in items if i.get("track_record_overall")), None)
print("track_record_overall:", json.dumps(tro))
st = [(i["symbol"], i["symbol_track_record"]) for i in items if i.get("symbol_track_record") and i["symbol_track_record"].get("total")]
print("\nsymbols with track record:", len(st))
for s, t in st[:4]:
    print(f"  {s}: {t.get('hits')}/{t.get('total')} acc={t.get('accuracy')}%")
    for o in (t.get("recent") or [])[:3]:
        print("     ", json.dumps(o)[:200])

# pulse history
p = d.get("pulse") or {}
print("\npulse keys:", list(p.keys()))
print("history len:", len(p.get("history") or []), "| history_meta:", p.get("history_meta"))
print("persisted:", p.get("persisted"))
if p.get("history"):
    h = p["history"][0]
    print("latest history entry:", json.dumps(h)[:400])

# confidence object for a conf=0 item
veco = next(i for i in items if i["symbol"] == "VECO")
print("\nVECO confidence obj:", json.dumps(veco.get("confidence"))[:300])
print("VECO confidence_score:", veco.get("confidence_score"), "| data_confidence:", veco.get("data_confidence"))
print("VECO ai_decision:", json.dumps(veco.get("ai_decision"))[:250])
print("VECO llm_direction_score:", veco.get("llm_direction_score"), "| structured:", veco.get("structured_direction_bias_score"))

# WULF: conf 0 but cov 66 — HIGH coverage but zero confidence?
wulf = next(i for i in items if i["symbol"] == "WULF")
print("\nWULF: cov=", wulf.get("coverage_score"), "conf=", wulf.get("confidence_score"), "gap=", wulf.get("gap_risk"), "label=", wulf.get("direction_label"), "score=", wulf.get("direction_bias_score"))
print("WULF data_message:", wulf.get("data_message"))

# data_message distribution for NONE gap
from collections import Counter
msgs = Counter(str(i.get("data_message"))[:80] for i in items if i.get("gap_risk") is None)
print("\ngap-NONE data_messages:")
for m, c in msgs.most_common(5): print(f"  [{c}] {m}")

# direction components sample for VECO (how is a -62 built with no data?)
print("\nVECO direction_components:", json.dumps(veco.get("direction_components"))[:400])
print("VECO components:", json.dumps(veco.get("components"))[:400])
print("VECO why:", veco.get("why"))
print("VECO ai_sentiment:", veco.get("ai_sentiment"))
