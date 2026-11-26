"""
Exit-replay backtest (bounded, data-driven).

Takes the REAL closed crypto trades (entries/SL/TP/direction/timestamps are known),
fetches each trade's actual 1m price path from Binance, and replays the exit logic
under different trailing-stop parameters to find the config that maximizes expectancy.

This does NOT re-generate signals — the entries are fixed (the bot's real entries).
It isolates the highest-leverage lever found in the diagnosis: the exit/R:R.

Fidelity guard: the CURRENT live params (V0) replay should reproduce the realized
crypto PnL/exit mix closely; if it doesn't, the replay is not trustworthy.
"""
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BINANCE = "https://api.binance.com/api/v3"
MAX_HOLD_MIN = 720  # bot.max_holding_minutes


def to_binance_symbol(ticker: str) -> str:
    t = ticker.upper().replace("-", "")
    if t.endswith("USD") and not t.endswith("USDT"):
        return t[:-3] + "USDT"
    return t


def fetch_klines(symbol, start_ms, end_ms):
    out = []
    cur = start_ms
    for _ in range(20):
        qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1m",
                                     "startTime": cur, "endTime": end_ms, "limit": 1000})
        try:
            with urllib.request.urlopen(f"{BINANCE}/klines?{qs}", timeout=10) as r:
                data = json.loads(r.read())
        except Exception:
            return None
        if not data:
            break
        out.extend(data)
        if len(data) < 1000:
            break
        cur = int(data[-1][0]) + 1
        if cur >= end_ms:
            break
        time.sleep(0.05)
    # [open_time, open, high, low, close, ...]
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in out]


def simulate_exit(entry, sl, tp, direction, bars, cfg, commission_rate, qty):
    """Replay exit logic over 1m bars. Returns (exit_price, reason, net_pnl)."""
    is_long = direction == "LONG"
    trailing_on = cfg.get("trailing_enabled", True)
    act_r = float(cfg.get("trailing_activation_r", 1.0))
    factor = float(cfg.get("trailing_factor", 0.5))
    risk = abs(entry - sl) if sl > 0 else entry * 0.02
    if risk <= 0:
        risk = entry * 0.02
    trailing_sl = 0.0
    max_fav = 0.0

    def close(px, reason):
        if is_long:
            gross = (px - entry) * qty
        else:
            gross = (entry - px) * qty
        comm = (entry * qty + px * qty) * commission_rate
        return px, reason, round(gross - comm, 6)

    for (_t, o, hi, lo, c) in bars:
        # 1) Hard SL first (conservative: adverse intrabar extreme)
        if is_long:
            if sl > 0 and lo <= sl:
                return close(sl, "stop_loss")
            if tp > 0 and hi >= tp:
                return close(tp, "take_profit")
        else:
            if sl > 0 and hi >= sl:
                return close(sl, "stop_loss")
            if tp > 0 and lo <= tp:
                return close(tp, "take_profit")
        # 2) Existing trailing SL hit (adverse extreme)
        if trailing_sl > 0:
            if is_long and lo <= trailing_sl:
                return close(trailing_sl, "trailing_stop")
            if not is_long and hi >= trailing_sl:
                return close(trailing_sl, "trailing_stop")
        # 3) Update trailing using favorable extreme
        if trailing_on:
            fav_px = hi if is_long else lo
            favorable = (fav_px - entry) if is_long else (entry - fav_px)
            max_fav = max(max_fav, favorable)
            cur_r = favorable / risk
            if cur_r >= act_r:
                if is_long:
                    new_sl = entry + favorable * factor
                    if cur_r >= 1.0 and new_sl < entry:
                        new_sl = entry
                    new_sl = max(new_sl, sl) if sl > 0 else new_sl
                    trailing_sl = max(trailing_sl, new_sl)
                else:
                    new_sl = entry - favorable * factor
                    new_sl = min(new_sl, sl) if sl > 0 else new_sl
                    trailing_sl = new_sl if trailing_sl <= 0 else min(trailing_sl, new_sl)
    # Held to end of window -> close at last close (time exit)
    last_close = bars[-1][4] if bars else entry
    return close(last_close, "time_exit")


def main():
    data = json.load(open("capital.json"))
    closed = [p for p in data.get("positions", []) if str(p.get("status", "")).upper() == "CLOSED"]
    crypto = [p for p in closed if str(p.get("ticker", "")).endswith("-USD")]

    variants = {
        "V0_current(act1.0,f0.5)": {"trailing_enabled": True, "trailing_activation_r": 1.0, "trailing_factor": 0.5},
        "V1_trailing_OFF": {"trailing_enabled": False},
        "V2_act1.5,f0.5": {"trailing_enabled": True, "trailing_activation_r": 1.5, "trailing_factor": 0.5},
        "V3_act2.0,f0.5": {"trailing_enabled": True, "trailing_activation_r": 2.0, "trailing_factor": 0.5},
        "V4_act1.0,f0.75": {"trailing_enabled": True, "trailing_activation_r": 1.0, "trailing_factor": 0.75},
        "V5_act2.0,f0.8": {"trailing_enabled": True, "trailing_activation_r": 2.0, "trailing_factor": 0.8},
        "V6_act1.5,f0.8": {"trailing_enabled": True, "trailing_activation_r": 1.5, "trailing_factor": 0.8},
    }
    agg = {k: {"pnl": 0.0, "w": 0, "n": 0, "wins_sum": 0.0, "loss_sum": 0.0, "reasons": {}} for k in variants}
    realized = {"pnl": 0.0, "w": 0, "n": 0}

    fetched = 0
    for p in crypto:
        try:
            sym = to_binance_symbol(p["ticker"])
            o_dt = datetime.fromisoformat(p["opened_at"]).astimezone(timezone.utc)
            start_ms = int(o_dt.timestamp() * 1000)
            end_ms = start_ms + MAX_HOLD_MIN * 60 * 1000
            bars = fetch_klines(sym, start_ms, end_ms)
            if not bars or len(bars) < 2:
                continue
            entry = float(p["entry_price"]); sl = float(p["stop_loss_price"]); tp = float(p["take_profit_price"])
            direction = str(p.get("direction", "LONG")).upper()
            qty = float(p.get("qty") or 0.0)
            comm = float(p.get("commission_rate") or 0.0)
            if comm <= 0:
                recorded_entry_fee = float(p.get("commission_usd") or 0.0)
                recorded_notional = float(p.get("usd_size") or (entry * qty) or 0.0)
                if recorded_entry_fee > 0 and recorded_notional > 0:
                    comm = recorded_entry_fee / recorded_notional
                else:
                    print("skip", p.get("ticker"), "missing recorded commission rate")
                    continue
            if qty <= 0:
                continue
            fetched += 1
            rp = float(p.get("net_pnl") or 0.0)
            realized["pnl"] += rp; realized["n"] += 1; realized["w"] += 1 if rp > 0 else 0
            for name, cfg in variants.items():
                _px, reason, npnl = simulate_exit(entry, sl, tp, direction, bars, cfg, comm, qty)
                a = agg[name]
                a["pnl"] += npnl; a["n"] += 1; a["reasons"][reason] = a["reasons"].get(reason, 0) + 1
                if npnl > 0:
                    a["w"] += 1; a["wins_sum"] += npnl
                else:
                    a["loss_sum"] += npnl
        except Exception as e:
            print("skip", p.get("ticker"), repr(e)[:60])
            continue

    print(f"\n=== Replayed {fetched}/{len(crypto)} crypto trades ===")
    print(f"REALIZED (actual): PnL=${realized['pnl']:.2f}  WR={realized['w']/realized['n']*100:.1f}%  n={realized['n']}")
    print(f"\n{'variant':24s} {'PnL':>9s} {'WR%':>6s} {'avgW':>8s} {'avgL':>8s} {'R:R':>5s} {'exp/trade':>10s}")
    rows = []
    for name, a in agg.items():
        n = a["n"] or 1
        w = a["w"]; losses = n - w
        avgw = a["wins_sum"] / w if w else 0.0
        avgl = a["loss_sum"] / losses if losses else 0.0
        rr = (avgw / abs(avgl)) if avgl else 0.0
        exp = a["pnl"] / n
        rows.append((a["pnl"], name, w / n * 100, avgw, avgl, rr, exp, a["reasons"]))
    for pnl, name, wr, avgw, avgl, rr, exp, reasons in sorted(rows, reverse=True):
        print(f"{name:24s} {pnl:9.2f} {wr:6.1f} {avgw:8.4f} {avgl:8.4f} {rr:5.2f} {exp:10.4f}")
    print("\nExit-reason mix per variant:")
    for pnl, name, *_rest, reasons in sorted(rows, reverse=True):
        print(f"  {name:24s} {reasons}")


if __name__ == "__main__":
    main()
