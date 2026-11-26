import json
with open("pe_live_8003_fresh.json", encoding="utf-8") as f:
    d = json.load(f)
p = d["pulse"]
print("SUMMARY:", p.get("summary"))
print("\nCATALYSTS:", str(p.get("catalysts"))[:400])
print("\nRISKS:", str(p.get("risks"))[:400])
print("\nfull top_short reasons:")
for x in p.get("top_short") or []:
    print(f"  {x.get('symbol')}: {x.get('reason')}")
# Check ACMR item detail for the contradiction
it = next(i for i in d["items"] if i["symbol"]=="ACMR")
print("\nACMR item: label=", it["direction_label"], "score=", it["direction_bias_score"], "gap=", it["gap_risk"], "event=", it["earnings_time_utc"])
print("ACMR ai:", (it.get("ai_decision") or {}).get("verdict"), (it.get("ai_decision") or {}).get("confidence"))
# RDW
it = next(i for i in d["items"] if i["symbol"]=="RDW")
print("RDW item: label=", it["direction_label"], "score=", it["direction_bias_score"], "gap=", it["gap_risk"], "event=", it["earnings_time_utc"])
