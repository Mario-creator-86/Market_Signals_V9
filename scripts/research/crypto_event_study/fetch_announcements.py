"""Download the history of Binance listing and delisting announcements (public CMS endpoint, no key).

usage: fetch_announcements.py <outdir>
Writes announcements.json: [{"catalog": "listing"|"delisting", "id", "code", "title", "release_ms"}], newest first.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
       "?type=1&catalogId={cat}&pageNo={page}&pageSize=50")
HEADERS = {"User-Agent": "Mozilla/5.0 (research script)", "Accept": "application/json", "clienttype": "web", "lang": "en"}
CATALOGS = {48: "listing", 161: "delisting"}


def get(cat: int, page: int):
    for attempt in range(4):
        try:
            req = urllib.request.Request(URL.format(cat=cat, page=page), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == 3:
                raise
            time.sleep(2.0 * (attempt + 1))


rows = []
for cat, name in CATALOGS.items():
    page, total = 1, None
    while True:
        data = get(cat, page)
        catalogs = (data.get("data") or {}).get("catalogs") or []
        if not catalogs:
            break
        total = catalogs[0].get("total") or 0
        articles = catalogs[0].get("articles") or []
        if not articles:
            break
        for a in articles:
            rows.append({"catalog": name, "id": a.get("id"), "code": a.get("code"),
                         "title": a.get("title") or "", "release_ms": int(a.get("releaseDate") or 0)})
        if page * 50 >= int(total):
            break
        page += 1
        time.sleep(0.4)
    print(f"{name}: {sum(1 for r in rows if r['catalog'] == name)} of {total}")

rows.sort(key=lambda r: -r["release_ms"])
(OUT / "announcements.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
print("saved", len(rows), "announcements to", OUT / "announcements.json")
