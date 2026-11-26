import json
with open("pe_track_record.json", encoding="utf-8") as f:
    d = json.load(f)
print("top-level keys:", list(d.keys()))
print(json.dumps({k:v for k,v in d.items() if not isinstance(v,(list,dict))}, indent=1)[:1500])
for k,v in d.items():
    if isinstance(v, list):
        print(f"\n{k}: list n={len(v)}")
        for row in v[:5]:
            print("  ", json.dumps(row)[:300])
    elif isinstance(v, dict):
        print(f"\n{k}: dict keys={list(v.keys())[:20]}")
        print("  ", json.dumps(v)[:600])
