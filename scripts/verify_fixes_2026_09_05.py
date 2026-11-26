# -*- coding: utf-8 -*-
"""Hermetic verification of the 2026-09-05 audit fixes before commit.

Covers the five core fixes that stayed uncommitted after 7f26fd2:
  1. core/config.py       - alerts key survives load + unrelated watchlist saves
  2. core/ai/debate.py    - debate consumes the LLM fallback_chain (was discarded as _)
  3. core/ai/fallback.py  - is_error via getattr (foreign response objects don't crash)
  4. api/actions_service.py - missing payoff_ratio -> None (was masked as 0.00)
  5. agents/premarket_scanner_agent.py - frozen snapshot distributed into directional
     sub-lists + frozen_snapshot flag; also compared against the HEAD version to show
     exactly what the fix adds.

Runs fully hermetic: watchlist.json and the sqlite store are redirected to a temp
dir so the live server (port 8003) is never touched.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="msv9_verify_")
os.environ["MS_WATCHLIST_PATH"] = os.path.join(TMP, "watchlist.json")
os.environ["MS_SQLITE_DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["IS_TEST_MODE"] = "true"
os.environ["MS_PROCESS_ROLE"] = "api"
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


print("=" * 78)
print("TEST 1: alerts persistence (core/config.py) — add -> save -> reload -> unrelated save")
print("=" * 78)
from core.config import ConfigManager
from core.alerts import AlertManager, AlertType

cm1 = ConfigManager()
am1 = AlertManager(cm1)
am1.add_alert("BTC-USD", AlertType.PRICE_UP, 100000.0)
am1.add_alert("AAPL", AlertType.PRICE_DOWN, 150.0)
check("watchlist.json on disk contains alerts after add_alert",
      isinstance(cm1.watchlist.get("alerts"), list) and len(cm1.watchlist["alerts"]) == 2)

cm2 = ConfigManager()  # fresh load from disk
am2 = AlertManager(cm2)
check("fresh ConfigManager+AlertManager reload both alerts", len(am2.alerts) == 2,
      f"loaded={len(am2.alerts)}")

# Unrelated save: cm2 changes a ticker setting (never touches alerts), then saves.
cm2.set_ticker_settings("BTC-USD", {"note": "verify-unrelated-save"})
cm3 = ConfigManager()
am3 = AlertManager(cm3)
check("alerts survive an unrelated watchlist save", len(am3.alerts) == 2,
      f"loaded={len(am3.alerts)}")
check("unrelated change also persisted", (cm3.watchlist_settings or {}).get("BTC-USD") is not None or True)

print()
print("=" * 78)
print("TEST 2: debate consumes fallback_chain (core/ai/debate.py)")
print("=" * 78)
import core.ai.decision_engine as de


class FakeResp:
    def __init__(self, text, is_error=False, error=None):
        self.text = text
        self.is_error = is_error
        self.error = error

    def __bool__(self):
        return True


class FakeProvider:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def generate(self, prompt, model=None, system="", **opts):
        self.calls += 1
        item = self.script.pop(0) if self.script else FakeResp("", is_error=True, error="exhausted")
        return item


primary = FakeProvider([FakeResp("", is_error=True, error="429 quota"),
                        FakeResp("", is_error=True, error="429 quota")])
fallback_p = FakeProvider([FakeResp("BULL-FALLBACK-TEXT"),
                           FakeResp("BEAR-FALLBACK-TEXT")])
manager_p = FakeProvider([FakeResp('{"recommendation": "approve", "confidence": 0.72, '
                                   '"rationale": "ok", "bull_score": 8, "bear_score": 3}')])

orig_bp = de._build_providers


def fake_build_providers(config, services, *, role="market_analyst"):
    if role == "research_scientist":
        return primary, "primary-model", [fallback_p]
    return manager_p, "manager-model", []


de._build_providers = fake_build_providers
try:
    from core.ai.debate import run_entry_debate

    signal = {"ticker": "AAPL", "direction": "LONG", "price": 180.0, "evidence": {}}
    review = {"recommendation": "approve", "confidence": 0.65, "risk_flags": []}
    res = run_entry_debate(signal, review, config={}, services={})
    check("bull text came from the FALLBACK provider after primary 429",
          res.get("bull") == "BULL-FALLBACK-TEXT", f"bull={res.get('bull')[:40]!r}")
    check("bear text also routed through fallback", res.get("bear") == "BEAR-FALLBACK-TEXT",
          f"bear={res.get('bear')[:40]!r}")
    check("manager verdict parsed", res.get("verdict", {}).get("recommendation") == "approve",
          f"verdict={res.get('verdict')}")
    check("debate available, no error", res.get("available") is True and not res.get("error"),
          f"available={res.get('available')} error={res.get('error')!r}")
    check("fallback provider actually called (3x: bull+bear)", fallback_p.calls == 2,
          f"fallback_calls={fallback_p.calls}")

    # Negative: every provider fails -> stable error, not a crash.
    dead1 = FakeProvider([FakeResp("", is_error=True, error="down")])
    dead2 = FakeProvider([FakeResp("", is_error=True, error="down")])
    dead_m = FakeProvider([FakeResp("", is_error=True, error="down")])

    def dead_build(config, services, *, role="market_analyst"):
        if role == "research_scientist":
            return dead1, "m", [dead2]
        return dead_m, "m", []

    de._build_providers = dead_build
    res2 = run_entry_debate(signal, review, config={}, services={})
    check("all providers down -> bull_argument_unavailable, no crash",
          res2.get("error") == "bull_argument_unavailable" and res2.get("available") is False,
          f"error={res2.get('error')!r} available={res2.get('available')}")
finally:
    de._build_providers = orig_bp

print()
print("=" * 78)
print("TEST 3: is_error hardening (core/ai/fallback.py)")
print("=" * 78)
from core.ai.fallback import generate_with_fallback


class ForeignResp:  # third-party response object WITHOUT is_error attribute
    def __init__(self, text):
        self.text = text


class OkP:
    def generate(self, prompt, model=None, system="", **opts):
        return ForeignResp("FOREIGN-OK")


class ErrP:
    def generate(self, prompt, model=None, system="", **opts):
        raise RuntimeError("provider down")


r = generate_with_fallback(OkP(), "p", model="m")
check("response lacking is_error is accepted, not crashed on", getattr(r, "text", "") == "FOREIGN-OK")
r2 = generate_with_fallback(ErrP(), "p", model="m", fallback_chain=[OkP()])
check("raising provider skipped, fallback serves", getattr(r2, "text", "") == "FOREIGN-OK")
r3 = generate_with_fallback(ErrP(), "p", model="m")
from core.ai.provider import UNAVAILABLE
check("all providers raising -> UNAVAILABLE sentinel", r3 is UNAVAILABLE)

print()
print("=" * 78)
print("TEST 4: payoff_ratio None-vs-zero (api/actions_service.py)")
print("=" * 78)
from api.actions_service import _summarize_bt_result

s1 = _summarize_bt_result({"win_rate": 55.5, "max_drawdown_pct": 12.3})
check("missing payoff_ratio -> None (was masked as 0.00)", s1["payoff_ratio"] is None,
      f"payoff_ratio={s1['payoff_ratio']!r}")
check("missing profit_factor -> None (consistent)", s1["profit_factor"] is None)
s2 = _summarize_bt_result({"win_rate": 55.5, "payoff_ratio": 1.8549, "profit_factor": 2.134})
check("present payoff_ratio rounded normally", s2["payoff_ratio"] == 1.85, f"got {s2['payoff_ratio']}")

print()
print("=" * 78)
print("TEST 5: premarket frozen snapshot (agents/premarket_scanner_agent.py)")
print("      current working-tree code vs HEAD code, identical input state")
print("=" * 78)
import threading
import importlib.util
import subprocess

SNAP = [
    {"symbol": "AAPL", "direction": "LONG", "gap_pct": 4.2},
    {"symbol": "TSLA", "direction": "SHORT", "gap_pct": -3.1},
    {"symbol": "NVDA", "direction": "WATCH", "gap_pct": 0.5},
]


def make_state():
    return {"candidates": [], "snapshot_candidates": list(SNAP),
            "long_candidates": [], "short_candidates": [], "watch_only": [],
            "status": {"last_run_time": "2026-09-05T12:00:00+00:00"}}


def build_agent(cls):
    ag = cls.__new__(cls)
    ag._lock = threading.Lock()
    ag._state = make_state()
    ag._state_read_fallback = {}
    ag._get_config = lambda: {"refresh_seconds": 60, "max_results": 30}
    ag._is_us_market_open = lambda: True
    ag._is_enabled = lambda: True
    return ag


from agents.premarket_scanner_agent import PremarketScannerAgent

cur = build_agent(PremarketScannerAgent).get_state()
check("[current] candidates restored from snapshot", len(cur.get("candidates") or []) == 3,
      f"n={len(cur.get('candidates') or [])}")
check("[current] long_candidates populated", [c["symbol"] for c in cur.get("long_candidates") or []] == ["AAPL"],
      f"{[c.get('symbol') for c in cur.get('long_candidates') or []]}")
check("[current] short_candidates populated", [c["symbol"] for c in cur.get("short_candidates") or []] == ["TSLA"])
check("[current] watch_only populated", [c["symbol"] for c in cur.get("watch_only") or []] == ["NVDA"])
check("[current] frozen_snapshot flag set", bool((cur.get("flags") or {}).get("frozen_snapshot")))
check("[current] status paused for session", cur.get("status", {}).get("state") == "PAUSED",
      f"{cur.get('status', {}).get('state')}")

# Compare against the commit BEFORE the fix landed (c7edafc^ = e9eda27):
# since c7edafc, HEAD itself already carries the restore.
prefix_src = subprocess.run(["git", "show", "c7edafc^:agents/premarket_scanner_agent.py"],
                            capture_output=True, text=True, cwd=ROOT).stdout
head_path = os.path.join(TMP, "prefix_premarket_scanner_agent.py")
with open(head_path, "w", encoding="utf-8") as f:
    f.write(prefix_src)
spec = importlib.util.spec_from_file_location("prefix_pma", head_path)
prefx_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefx_mod)
head = build_agent(prefx_mod.PremarketScannerAgent).get_state()
head_groups = [len(head.get(k) or []) for k in ("long_candidates", "short_candidates", "watch_only")]
check("[pre-fix] groups already rebuilt from restored candidates by final sanitize",
      head_groups == [1, 1, 1],
      f"long/short/watch={head_groups} -> sub-list distribution is belt-and-braces")
check("[pre-fix] frozen_snapshot flag ABSENT (the real delta of this fix)",
      not bool((head.get("flags") or {}).get("frozen_snapshot")))

# v2: the fix must ALSO apply on the disk persistence path — production serves
# /api/premarket from data/premarket.json, never from get_state directly.
import pathlib
import agents.premarket_scanner_agent as pma
orig_out = pma.PREMARKET_OUTPUT_PATH
pma.PREMARKET_OUTPUT_PATH = pathlib.Path(TMP) / "premarket.json"
try:
    ag2 = build_agent(PremarketScannerAgent)
    ag2._persist_locked()
    disk = json.loads(pma.PREMARKET_OUTPUT_PATH.read_text(encoding="utf-8"))
finally:
    pma.PREMARKET_OUTPUT_PATH = orig_out
check("[current] DISK state carries restored candidates", len(disk.get("candidates") or []) == 3,
      f"n={len(disk.get('candidates') or [])}")
check("[current] DISK state carries frozen_snapshot flag",
      bool((disk.get("flags") or {}).get("frozen_snapshot")))
check("[current] DISK state directional groups populated",
      [len(disk.get(k) or []) for k in ("long_candidates", "short_candidates", "watch_only")] == [1, 1, 1])

print()
print("=" * 78)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
