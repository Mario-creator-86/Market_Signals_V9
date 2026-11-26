import json
from datetime import datetime, timezone, timedelta
from collections import Counter

with open("pe_live_8003_fresh.json", encoding="utf-8") as f:
    d = json.load(f)
items = d["items"]
now = datetime.now(timezone.utc)
today = now.date()

genuinely_past, today_rows, future_rows = [], [], []
for i in items:
    et = datetime.fromisoformat(i["earnings_time_utc"].replace("Z","+00:00"))
    if et.date() < today: genuinely_past.append((i["symbol"], i["earnings_time_utc"], i["session"], i.get("direction_label"), (i.get("flags") or {}).get("already_reported")))
    elif et.date() == today: today_rows.append((i["symbol"], i["earnings_time_utc"], i["session"], i.get("direction_label"), i.get("gap_risk"), i.get("gap_risk_level")))
    else: future_rows.append(i)

print("now (UTC):", now.isoformat(), " today:", today)
print("\n-- genuinely PAST dates still in items:", len(genuinely_past))
for r in sorted(genuinely_past, key=lambda x:x[1]): print("  ", r)
print("\n-- TODAY rows:", len(today_rows))
sess = Counter(r[2] for r in today_rows)
print("  sessions:", dict(sess))
for r in sorted(today_rows, key=lambda x:x[1])[:40]: print("  ", r)
print("\n-- future rows:", len(future_rows))
fc = Counter(i["earnings_time_utc"][:10] for i in future_rows)
for k in sorted(fc): print("  ", k, fc[k])

# exact time vs midnight
midnight = sum(1 for i in items if i["earnings_time_utc"][11:19] == "00:00:00")
noon = sum(1 for i in items if i["earnings_time_utc"][11:19] == "12:00:00")
print("\nrows at 00:00:00Z:", midnight, "| rows at 12:00:00Z:", noon, "| other:", len(items)-midnight-noon)
# session vs time cross-check
for i in items:
    t = i["earnings_time_utc"][11:19]
    if t == "12:00:00" and i["session"] != "BMO":
        print("  odd: 12:00Z but session", i["symbol"], i["session"])

# how many today's BMO rows already 'reportable' per rule (now > 14:30 UTC?)
print("\nnow > 14:30 UTC?", now.hour > 14 or (now.hour == 14 and now.minute >= 30))
