import json
from collections import Counter
with open("pe_track_record.json", encoding="utf-8") as f:
    d = json.load(f)
recent = d["outcomes_recent"]
# verdict shown by UI: predicted_ai_verdict or bias>0 LONG / <0 SHORT / ==0 NEUTRAL
# but backend hit threshold is ±15. Check rows where 0<|bias|<15 but hit is not None
bad = []
shown_counter = Counter()
for o in recent:
    b = o.get("predicted_direction_bias") or 0
    v = o.get("predicted_ai_verdict") or ("LONG" if b > 0 else ("SHORT" if b < 0 else "NEUTRAL"))
    shown_counter[v] += 1
    if 0 < abs(b) < 15 and o.get("hit") is not None:
        bad.append((o["symbol"], b, o.get("hit"), o.get("realized_signed_move_pct")))
print("verdict badges as UI will show them:", dict(shown_counter))
print("rows with |bias|<15 but scored hit (threshold conflict):", len(bad))
for r in bad[:10]: print("  ", r)
# how many rows show LONG/SHORT badge despite |bias|<15
weak = [(o["symbol"], o.get("predicted_direction_bias"), o.get("hit")) for o in recent if 0 < abs(o.get("predicted_direction_bias") or 0) < 15]
print("rows with 0<|bias|<15 (UI shows LONG/SHORT badge, tab elsewhere says NEUTRAL):", len(weak))
for r in weak[:12]: print("  ", r)
# realized move distribution sanity
moves = [o.get("realized_signed_move_pct") for o in recent if o.get("realized_signed_move_pct") is not None]
print("\nrealized moves: n=", len(moves), "min=", min(moves), "max=", max(moves))
# ai verdict null count
print("predicted_ai_verdict null:", sum(1 for o in recent if o.get("predicted_ai_verdict") is None), "/", len(recent))
print("ai_coverage:", d.get("ai_coverage"), "sample_size:", d.get("sample_size"), "overall.total:", d["overall"]["total"])
