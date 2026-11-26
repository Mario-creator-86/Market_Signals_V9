"""Read-only audit of archived calls against independently fetched daily bars.

This is a threshold sensitivity check, NOT a full point-in-time policy replay.
Never starts the agent, places orders, or rewrites application data.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def event_gap(bars, event_date, session):
    """Require the event session and its adjacent trading session; no stale bars."""
    rows = sorted(bars, key=lambda r: r["date"])
    at = next((i for i, r in enumerate(rows) if r["date"] == event_date), None)
    if at is None or session not in ("BMO", "AMC"):
        return {"price_status": "missing_event_session"}
    left, right = (at - 1, at) if session == "BMO" else (at, at + 1)
    if left < 0 or right >= len(rows):
        return {"price_status": "missing_adjacent_session"}
    before, after = rows[left], rows[right]
    days = (datetime.fromisoformat(after["date"]) - datetime.fromisoformat(before["date"])).days
    if not 1 <= days <= 4:
        return {"price_status": "nonadjacent_sessions"}
    close, opening = before.get("Close"), after.get("Open")
    if not close or not opening or not math.isfinite(close) or not math.isfinite(opening) or close <= 0 or opening <= 0:
        return {"price_status": "invalid_prices"}
    return {"price_status": "bars_available_event_identity_unverified", "reference_date": before["date"],
            "reaction_date": after["date"], "reference_close": close, "reaction_open": opening,
            "provider_gap_pct": (opening / close - 1) * 100,
            "split_on_reaction": after.get("Stock Splits", 0)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true", help="Fetch Yahoo daily bars via yfinance")
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    sources = {}
    snapshots = {}
    for name in ("pre_earnings_feedback", "pre_earnings"):
        archived = out / (name + "_snapshot.json")
        raw = archived.read_bytes() if archived.exists() else (ROOT / "data" / (name + ".json")).read_bytes()
        sources[name] = hashlib.sha256(raw).hexdigest()
        snapshots[name] = json.loads(raw)
        (out / (name + "_snapshot.json")).write_bytes(raw)
    if (out / "current_selection.json").exists():
        now = datetime.fromisoformat(json.loads((out / "current_selection.json").read_text(encoding="utf-8"))["evaluated_at_utc"])
    feedback = snapshots["pre_earnings_feedback"]
    calls = [r for r in feedback["outcomes"] if r.get("was_top_prediction") and r.get("top_prediction_direction") in ("LONG", "SHORT")]
    os.environ["TEST_MODE"] = "1"
    os.environ["MS_DISABLE_ENGINE_AUTOSTART"] = "1"
    from agents.pre_earnings_agent import PreEarningsAgent
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent.config_manager = SimpleNamespace(config=json.loads((ROOT / "config.json").read_text(encoding="utf-8")))
    cfg = agent._get_config()
    config_archive = out / "selection_config.json"
    if config_archive.exists():
        cfg = json.loads(config_archive.read_text(encoding="utf-8"))
    else:
        public_cfg = {k: v for k, v in cfg.items() if not any(word in k.lower() for word in ("api_key", "password", "secret", "token", "credential"))}
        config_archive.write_text(json.dumps(public_cfg, indent=2), encoding="utf-8")
    sources["selection_config"] = hashlib.sha256(config_archive.read_bytes()).hexdigest()
    items = copy.deepcopy(snapshots["pre_earnings"].get("items") or [])
    for item in items:
        agent._synchronize_direction_decision(item, cfg)
    top = agent._build_top_predictions(items, cfg, now=now)
    minimum = cfg["top_prediction_min_trader_score"]
    eligible = [r for r in items if r.get("recommendation_eligible")]
    current = {"evaluated_at_utc": now.isoformat(), "snapshot_asof": snapshots["pre_earnings"].get("as_of_utc"),
               "items": len(items), "eligible_count": len(eligible), "eligible_unique_symbols": len({r["symbol"] for r in eligible}),
               "minimum_score": minimum, "top_predictions": top,
               "all_eligible": [{k: r.get(k) for k in ("symbol", "direction_label", "trader_score", "earnings_time_utc", "session")} for r in eligible],
               "blockers": dict(Counter(b for r in items for b in r.get("recommendation_blockers", [])))}
    (out / "current_selection.json").write_text(json.dumps(current, indent=2, default=str), encoding="utf-8")
    cache = out / "provider_bars.json"
    if args.fetch:
        import yfinance as yf
        symbols = sorted({r["symbol"] for r in calls})
        start = (datetime.fromisoformat(min(r["event_utc"] for r in calls)).date() - timedelta(days=7)).isoformat()
        end = (datetime.fromisoformat(max(r["event_utc"] for r in calls)).date() + timedelta(days=6)).isoformat()
        frame = yf.download(symbols, start=start, end=end, auto_adjust=False, actions=True, group_by="ticker", threads=4, progress=False, timeout=20)
        payload = {"source": "Yahoo Finance via yfinance", "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                   "start": start, "end_exclusive": end, "auto_adjust": False, "symbols": {}}
        for symbol in symbols:
            try:
                df = frame[symbol].dropna(subset=["Open", "Close"])
                payload["symbols"][symbol] = [{"date": index.date().isoformat(), **{str(k): float(v) for k, v in row.items() if v == v}} for index, row in df.iterrows()]
            except (KeyError, ValueError):
                payload["symbols"][symbol] = []
        cache.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    provider = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {"symbols": {}}
    rows = []
    for call in calls:
        score = call.get("top_prediction_trader_score")
        side = call["top_prediction_direction"]
        row = {"symbol": call["symbol"], "event_date": call["event_utc"][:10], "session": call.get("session"),
               "captured_at_utc": call.get("top_prediction_first_seen_utc"), "side": side, "frozen_score": score,
               "passes_score_threshold": isinstance(score, (int, float)) and score >= minimum,
               "saved_gap_pct": call.get("realized_signed_move_pct"), "saved_hit": call.get("top_prediction_hit"),
               "full_policy_replay_available": False}
        row.update(event_gap(provider["symbols"].get(row["symbol"], []), row["event_date"], row["session"]))
        production_bars = [{"dt": datetime.fromisoformat(b["date"]).replace(tzinfo=timezone.utc),
                            "open": b["Open"], "close": b["Close"]}
                           for b in provider["symbols"].get(row["symbol"], [])]
        row["production_gap_pct"] = agent._compute_event_gap_move_pct(
            row["symbol"], datetime.fromisoformat(call["event_utc"]), row["session"], bars=production_bars)
        if "provider_gap_pct" in row:
            gap = row["provider_gap_pct"]
            row["provider_direction_hit"] = gap > 0 if side == "LONG" else gap < 0
            row["difference_from_saved_pp"] = gap - row["saved_gap_pct"] if row["saved_gap_pct"] is not None else None
        rows.append(row)
    selected = [r for r in rows if r["passes_score_threshold"]]
    comparable = [r for r in selected if "provider_gap_pct" in r and not r.get("split_on_reaction")]
    hits = sum(r["provider_direction_hit"] for r in comparable)
    report = {"generated_at_utc": now.isoformat(), "input_sha256": sources, "archived_top_calls": len(rows),
              "threshold_pass_count": len(selected), "saved_hits_all": sum(r["saved_hit"] is True for r in rows),
              "saved_hits_threshold": sum(r["saved_hit"] is True for r in selected),
              "provider_price_available": sum("provider_gap_pct" in r for r in rows),
              "threshold_no_split_price_checked_count": len(comparable),
              "threshold_no_split_price_checked_hits": hits,
              "threshold_no_split_always_long_hits": sum(r["provider_gap_pct"] > 0 for r in comparable),
              "production_gap_matches_independent_formula": all(
                  r["production_gap_pct"] is not None and abs(r["production_gap_pct"] - r["provider_gap_pct"]) < 1e-8
                  for r in rows if "provider_gap_pct" in r),
              "limitations": ["Archived threshold filter only; full historical gate inputs missing.",
                              "Daily bar checks alone do not verify earnings event identity or exact release time.",
                              "Saved results may contain corporate-action artifacts; not trading P/L."], "rows": rows}
    def percent(h, n):
        return f"{h/n*100:.2f}%" if n else "N/A"
    uncertainty = "Eșantion gol; precizia nu poate fi estimată."
    if comparable:
        n = len(comparable)
        proportion = hits / n
        z = 1.96
        middle = (proportion + z*z/(2*n)) / (1+z*z/n)
        radius = z * math.sqrt(proportion*(1-proportion)/n + z*z/(4*n*n)) / (1+z*z/n)
        longs = [r for r in comparable if r['side'] == 'LONG']
        shorts = [r for r in comparable if r['side'] == 'SHORT']
        uncertainty = (f"Pentru {hits}/{n}, intervalul Wilson 95% este aproximativ {(middle-radius)*100:.1f}%–{(middle+radius)*100:.1f}%. "
                       f"LONG: {sum(r['provider_direction_hit'] for r in longs)}/{len(longs)}; "
                       f"SHORT: {sum(r['provider_direction_hit'] for r in shorts)}/{len(shorts)}. ")
    markdown = [
        "# Reaudit Pre-Earnings — 7 septembrie 2026", "",
        "## Verdict", "",
        "Implementarea a fost verificată și corectată, dar performanța predictivă nu este validată. "
        "Nu este justificată afirmația că sistemul este acum precis sau complet verificat.", "",
        "## Selecția curentă", "",
        f"Snapshot {current['snapshot_asof']}; evaluare {now.isoformat()}. "
        f"{len(items)} rânduri analizate, {len(eligible)} eligibile, {len(top)} afișate în Top. Prag {minimum}/100.", "",
        "Acestea sunt rezultate ale filtrelor pe datele salvate, nu confirmări independente ale tuturor evenimentelor. "
        "CLGN este eligibil tehnic, însă earnings-ul atribuit datei de 8 septembrie nu este confirmat de pagina IR verificată. "
        "Nu îl considerăm candidat verificat pentru earnings.", "",
        "| Ticker | Direcție | Scor | Data din calendar | În Top 10 |", "|---|---|---:|---|---|",
    ]
    top_symbols = {r['symbol'] for r in top}
    for r in sorted(eligible, key=lambda r: -r['trader_score']):
        markdown.append(f"| {r['symbol']} | {r['direction_label']} | {r['trader_score']} | {r['earnings_time_utc'][:10]} | {'Da' if r['symbol'] in top_symbols else 'Nu'} |")
    markdown += ["", "## Test istoric real: ce poate și ce nu poate demonstra", "",
        f"Arhiva are {len(feedback['outcomes'])} rezultate și {len(calls)} recomandări Top cu direcția/scorul salvate. "
        f"{len(selected)} trec pragul de {minimum}, aplicat retrospectiv scorului salvat. "
        "Nu am recalculat știrile sau AI-ul istoric cu informațiile de astăzi și nu am ales pragul după rezultate.", "",
        "Snapshoturile noi first_direction_evidence/top_direction_evidence lipsesc din toate rezultatele și predicțiile deschise ale acestei arhive. "
        "Prin urmare nu putem reconstrui integral filtrele actuale la data fiecărei decizii. "
        "Testul este o verificare a prețurilor și o analiză a pragului pe recomandările arhivate; nu un backtest integral al politicii noi.", "",
        "Prețuri descărcate de la Yahoo Finance prin yfinance, daily, auto_adjust=False, actions=True. "
        "BMO: închiderea ședinței anterioare → deschiderea din ziua evenimentului. "
        "AMC: închiderea zilei evenimentului → deschiderea ședinței următoare. "
        "Istoricul daily Yahoo este ajustat pentru split; nu aplicăm încă o dată raportul splitului. "
        "Nu sunt randamente ale unor ordine executate, nu includ costuri, slippage sau taxe de împrumut.", "",
        "| Cohortă | Corecte / total | Rată |", "|---|---:|---:|",
        f"| Top arhivat, rezultate salvate | {report['saved_hits_all']}/{len(rows)} | {percent(report['saved_hits_all'], len(rows))} |",
        f"| Prag nou, rezultate salvate | {report['saved_hits_threshold']}/{len(selected)} | {percent(report['saved_hits_threshold'], len(selected))} |",
        f"| Prag nou, prețuri reverificate, fără split | {hits}/{len(comparable)} | {percent(hits, len(comparable))} |",
        f"| Mereu LONG, aceeași cohortă fără split | {report['threshold_no_split_always_long_hits']}/{len(comparable)} | {percent(report['threshold_no_split_always_long_hits'], len(comparable))} |", "",
        uncertainty +
        "Nu există suficientă evidență pentru performanța SHORT sau pentru un avantaj statistic. "
        "Identitatea și ora exactă a fiecărui earnings nu au fost verificate independent pentru toate cele 39 de rânduri.", "",
        "## Fiecare candidat istoric care trece pragul", "",
        "Mișcarea reprezintă variația acțiunii, inclusiv pentru SHORT; semnul negativ confirmă un SHORT. "
        "CLGN este prezentat separat în tabel, dar exclus din cohorta fără split.", "",
        "| Ticker | Data | Sesiune | Semnal | Scor salvat | Gap daily verificat | Direcție corectă | Observație |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for r in selected:
        gap = r.get('provider_gap_pct')
        result = 'Da' if r.get('provider_direction_hit') else 'Nu'
        note = 'EXCLUS: split, earnings neconfirmat' if r.get('split_on_reaction') else 'Preț verificat'
        gap_text = f'{gap:+.2f}%' if gap is not None else 'N/A'
        markdown.append(f"| {r['symbol']} | {r['event_date']} | {r['session']} | {r['side']} | {r['frozen_score']} | {gap_text} | {result} | {note} |")
    markdown += ["", "## Probleme găsite și corectate în cod", "",
        "1. Timeout pe lock: nu se mai citește starea fără lock și nu se mai eliberează un lock neobținut; endpoint-ul întoarce 503 cu Retry-After.",
        "2. Dovezile înghețate sunt asociate exclusiv evenimentului exact, cu normalizarea fusului orar; nu se mai împrumută alt eveniment aflat la până la 14 zile distanță. Sunt căutate și rezultatele deja încheiate.",
        "3. Eșecul recalculării dosarului blochează eligibilitatea în răspuns în loc să păstreze o eligibilitate veche.",
        "4. UI/API/cache folosesc identitatea evenimentului. Răspunsurile întârziate ale aceluiași ticker nu mai suprascriu alt eveniment.",
        "5. Evaluarea viitoare a gapului folosește loaderul daily existent, cu cache și buget de API, în locul barelor intraday. Sesiunea necunoscută, ziua absentă și distanța >4 zile între referințe sunt refuzate.",
        f"6. Formula de producție a fost comparată cu formula independentă pe toate cele {len(rows)} seturi reale de prețuri: {report['production_gap_matches_independent_formula']}.", "",
        "## Anomalii și limitări rămase", "",
        "- CLGN: arhiva salvată +859,420277%; barele daily 3,410000 → 3,309999 arată -2,932555%. "
        "Emitentul confirmă reverse split 1:10 la deschiderea din 4 septembrie. Nu tratăm diferența drept un profit demonstrat și nu reclasificăm automat evenimentul ca earnings.",
        "- GIII își schimbă semnul rezultatului față de arhivă (+0,84% → -0,53%), iar CIEN (-0,02% → +0,09%). Ambele au scor 59 și nu intră în cohorta de 23.",
        "- Istoricul live nu a fost rescris: procesul existent deține starea în memorie și poate suprascrie modificări externe. "
        "Calculele și excluderile corectate sunt în acest raport/CSV. Statisticile vechi din aplicație necesită o migrare controlată a rezultatelor cu proveniență.",
        "- Nu a fost repornit procesul existent care rulează botul. Testele rulează cu stocare izolată. Codul nou trebuie încărcat la restart pentru a intra în procesul local.",
        "- Datele noi trebuie să captureze toate intrările necesare unui replay. Snapshoturile de evidență actuale sunt utile pentru explicații, dar nu reprezintă încă un set complet de antrenare point-in-time.", "",
        "## Surse primare verificate", "",
        "- [CollPlant: reverse split, efectiv 4 septembrie](https://ir.collplant.com/news-events/press-releases/detail/203/collplant-announces-reverse-share-split).",
        "- [CollPlant: registrul comunicatelor](https://ir.collplant.com/news-events/press-releases): comunicările din 3–4 septembrie privesc achiziția/consiliul, fără confirmarea unui earnings în datele analizate.",
        "- [NVIDIA: rezultate 26 august 2026](https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-Financial-Results-for-Second-Quarter-Fiscal-2027/default.aspx).",
        "- [SentinelOne: anunț anterior, 27 august după închidere](https://investors.sentinelone.com/press-releases/news-details/2026/SentinelOne-Announces-Date-of-Fiscal-Second-Quarter-2027-Financial-Results-Conference-Call-and-Participation-in-Upcoming-Investor-Conference/default.aspx).",
        "- [Dell: rezultate 1 septembrie 2026](https://investors.delltechnologies.com/news-releases/news-release-details/dell-technologies-delivers-second-quarter-fiscal-2027-financial).",
        "- [Lululemon: rezultate 3 septembrie 2026](https://corporate.lululemon.com/newsroom/press-releases/2026/09-03-2026-210528733).", "",
        "## Reproducere și continuare", "",
        "`python -X utf8 scripts/verify_pre_earnings_history.py --output-dir docs/product_audit_2026-09-06/history_verification_2026-09-07`", "",
        "Reutilizează snapshoturile și prețurile arhivate. Pentru o evaluare nouă utilizați alt director și `--fetch`. "
        "Hashurile intrărilor sunt în history_verification.json; toate cele 39 de apeluri în historical_calls.csv, iar barele în provider_bars.json.", "",
        "Pașii următori: validarea datei/release-ului cu sursă primară și nivel de încredere explicit; migrare versionată a rezultatelor legacy; "
        "snapshoturi complete înainte de publicare; evaluare prospectivă LONG/SHORT, cu baseline și intervale de încredere. "
        "Nu modificați ponderile după aceste 22 de rezultate.",
    ]
    (out / "REAUDIT_REPORT.md").write_text('\n'.join(markdown) + '\n', encoding='utf-8')
    (out / "history_verification.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    import csv
    with (out / "historical_calls.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    print(json.dumps({k: v for k, v in current.items() if k != "top_predictions"}, indent=2))
    print("TOP", [(r["symbol"], r["side"], r["trader_score"]) for r in top])


if __name__ == "__main__":
    main()
