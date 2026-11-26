"""One-shot data-quality audit of the persisted pre_earnings.json snapshot."""
import json, math, sys
from collections import Counter
from pathlib import Path

p = Path("data/pre_earnings.json")
raw = p.read_text(encoding="utf-8")
data = json.loads(raw)

issues = []

def walk_bad_numbers(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_bad_numbers(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_bad_numbers(v, f"{path}[{i}]")
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            issues.append(f"BAD NUMBER at {path}: {obj}")

walk_bad_numbers(data)

items = data.get("items") or []
print(f"items: {len(items)}")
print(f"top-level keys: {sorted(data.keys())}")
print(f"as_of_utc: {data.get('as_of_utc')}")
status = data.get("status") or {}
print(f"status.last_run_time: {status.get('last_run_time')}")
print(f"status.last_success_time: {status.get('last_success_time')}")
print(f"status.last_error: {status.get('last_error')}")
print(f"disabled: {data.get('disabled')}")

# label / score coherence checks
mism_label = 0
mism_sign = 0
blend_mismatch = 0
n_ai = 0
n_ai_used = 0
for it in items:
    sym = it.get("symbol")
    score = it.get("direction_bias_score")
    label = it.get("direction_label")
    if isinstance(score, (int, float)) and label:
        expect = "LONG" if score >= 15 else ("SHORT" if score <= -15 else "NEUTRAL")
        if label != expect:
            mism_label += 1
            if mism_label <= 8:
                issues.append(f"LABEL MISMATCH {sym}: score={score} label={label} expected={expect}")
        fb = it.get("final_direction_bias_score")
        if isinstance(fb, (int, float)) and fb != score:
            blend_mismatch += 1
    ai = it.get("ai_decision") or {}
    if ai:
        n_ai += 1
        if ai.get("used"):
            n_ai_used += 1

print(f"\nlabel mismatches: {mism_label}")
print(f"final!=direction_bias_score count: {blend_mismatch}")
print(f"ai_decision blocks: {n_ai}, used: {n_ai_used}")

# gap risk coherence
gr_bad = 0
for it in items:
    gr = it.get("gap_risk_numeric")
    lvl = it.get("gap_risk_level")
    if isinstance(gr, (int, float)) and lvl:
        exp = "LOW" if gr < 40 else ("MEDIUM" if gr < 70 else "HIGH")
        if str(lvl).upper() != exp:
            gr_bad += 1
            if gr_bad <= 8:
                issues.append(f"GAP LEVEL MISMATCH {it.get('symbol')}: numeric={gr} level={lvl} expected={exp}")
print(f"gap level mismatches: {gr_bad}")

# duplicates by symbol
syms = [it.get("symbol") for it in items]
dup = [s for s, c in Counter(syms).items() if c > 1]
print(f"duplicate symbols: {dup}")

# earnings time sanity
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
past, future, missing_t = 0, 0, 0
for it in items:
    t = it.get("earnings_time_utc")
    if not t:
        missing_t += 1
        continue
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < now:
            past += 1
        else:
            future += 1
    except Exception:
        issues.append(f"UNPARSEABLE earnings_time_utc {it.get('symbol')}: {t}")
print(f"earnings past/future/missing: {past}/{future}/{missing_t}")

# coverage / confidence ranges
bad_range = 0
for it in items:
    for f in ("coverage_score", "confidence_score", "opportunity_score"):
        v = it.get(f)
        if isinstance(v, (int, float)) and not (0 <= v <= 100):
            bad_range += 1
            issues.append(f"RANGE {it.get('symbol')} {f}={v}")
print(f"out-of-range coverage/confidence/opportunity: {bad_range}")

# pulse
pulse = data.get("pulse")
print(f"\npulse present: {pulse is not None}")
if pulse:
    print(f"pulse keys: {sorted(pulse.keys())}")
    print(f"pulse sentiment: {pulse.get('sentiment')}, top_long={len(pulse.get('top_long') or [])}, top_short={len(pulse.get('top_short') or [])}")
    active_syms = {it.get('symbol') for it in items}
    for side in ("top_long", "top_short"):
        for pick in (pulse.get(side) or []):
            ps = pick.get("symbol") or pick.get("ticker")
            if ps and ps not in active_syms:
                issues.append(f"PULSE PICK {ps} in {side} not among active items")

print(f"\nreported_today count: {len(data.get('reported_today') or [])}")
print(f"flags: {data.get('flags')}")

print("\n================ ISSUES ================")
for i in issues[:60]:
    print(" -", i)
print(f"total issues: {len(issues)}")
