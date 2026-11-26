"""Backtest entry regimes for 24h crypto movers on hourly Binance data.

Costs follow the app's CRYPTO_FUTURES settings: 4 bps commission per side,
2.5 bps entry slippage, 5 bps stop slippage, 2.5 bps target/time slippage.
A bar that touches both stop and target counts as a stop (pessimistic).
One open trade per symbol; a new entry is possible from the bar after the exit.
"""
import json
import sys
from pathlib import Path

import numpy as np

DATA = Path(sys.argv[1])
MIN_VOL = 10_000_000.0
COMM, SLIP_IN, SLIP_SL, SLIP_OUT = 0.0004, 0.00025, 0.0005, 0.00025


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = np.zeros_like(close), np.zeros_like(close)
    au[n] = up[1:n + 1].mean(); ad[n] = dn[1:n + 1].mean()
    for i in range(n + 1, len(close)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    out = 100 - 100 / (1 + au / np.where(ad == 0, 1e-12, ad))
    out[:n] = 50
    return out


def roll(a: np.ndarray, n: int, fn) -> np.ndarray:
    out = np.full_like(a, np.nan)
    if len(a) >= n:
        w = np.lib.stride_tricks.sliding_window_view(a, n)
        out[n - 1:] = fn(w, axis=1)
    return out


def load():
    meta = json.loads((DATA / "_symbols.json").read_text())
    series = {}
    for sym in meta["symbols"]:
        arr = np.loadtxt(DATA / f"{sym}.csv", delimiter=",", skiprows=1)
        if arr.ndim != 2 or len(arr) < 24 * 20:
            continue
        t, o, h, l, c, qv = arr.T
        s = {"t": t.astype(np.int64), "o": o, "h": h, "l": l, "c": c}
        s["chg24"] = np.full_like(c, np.nan); s["chg24"][24:] = c[24:] / c[:-24] - 1
        s["chg4"] = np.full_like(c, np.nan); s["chg4"][4:] = c[4:] / c[:-4] - 1
        s["chg1"] = np.full_like(c, np.nan); s["chg1"][1:] = c[1:] / c[:-1] - 1
        s["chg72"] = np.full_like(c, np.nan); s["chg72"][72:] = c[72:] / c[:-72] - 1
        s["vol24"] = roll(qv, 24, np.sum)
        v7 = roll(qv, 168, np.sum) / 7.0
        s["relvol"] = s["vol24"] / np.where(v7 > 0, v7, np.nan)
        s["hi24"] = roll(h, 24, np.max); s["lo24"] = roll(l, 24, np.min)
        s["pull_hi"] = (s["hi24"] - c) / s["hi24"]          # how far below the 24h high
        s["bounce_lo"] = (c - s["lo24"]) / s["lo24"]        # how far above the 24h low
        s["rsi"] = rsi(c)
        series[sym] = s
    btc = series["BTCUSDT"]
    bmap24 = dict(zip(btc["t"].tolist(), btc["chg24"].tolist()))
    bmap4 = dict(zip(btc["t"].tolist(), btc["chg4"].tolist()))
    for s in series.values():
        s["btc24"] = np.array([bmap24.get(x, np.nan) for x in s["t"].tolist()])
        s["btc4"] = np.array([bmap4.get(x, np.nan) for x in s["t"].tolist()])
    return series


def simulate(series, cond, direction, sl, rr, hold):
    """cond(s) -> bool mask; direction(s) -> +1 long / -1 short array. Returns trades."""
    trades = []
    for sym, s in series.items():
        if sym == "BTCUSDT":
            continue
        mask = cond(s) & (s["vol24"] >= MIN_VOL) & np.isfinite(s["chg24"])
        side = direction(s)
        idx = np.flatnonzero(mask)
        n = len(s["c"])
        free_from = 0
        for i in idx:
            if i < free_from or i + 1 >= n:
                continue
            d = 1 if side[i] > 0 else -1
            entry = s["c"][i]
            stop = entry * (1 - sl * d)
            target = entry * (1 + sl * rr * d)
            exit_price, why, j = None, None, i
            for j in range(i + 1, min(n, i + 1 + hold)):
                hi, lo = s["h"][j], s["l"][j]
                hit_sl = lo <= stop if d > 0 else hi >= stop
                hit_tp = hi >= target if d > 0 else lo <= target
                if hit_sl:
                    exit_price, why = stop, "sl"; break
                if hit_tp:
                    exit_price, why = target, "tp"; break
            if exit_price is None:
                exit_price, why = s["c"][j], "time"
            gross = (exit_price / entry - 1) * d
            net = gross - 2 * COMM - SLIP_IN - (SLIP_SL if why == "sl" else SLIP_OUT)
            trades.append((int(s["t"][i]), sym, d, net, why, j - i))
            free_from = j + 1
    return trades


def stats(trades, t_split=None, part=None):
    sel = trades
    if t_split is not None:
        sel = [x for x in trades if (x[0] < t_split) == (part == "train")]
    if not sel:
        return None
    r = np.array([x[3] for x in sel])
    pos, neg = r[r > 0].sum(), -r[r <= 0].sum()
    days = {}
    for x in sel:
        days.setdefault(x[0] // 86_400_000, []).append(x[3])
    daily = np.array([np.sum(v) for v in days.values()])
    tstat = daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))) if len(daily) > 2 and daily.std(ddof=1) > 0 else 0.0
    return {"n": len(r), "win": 100 * (r > 0).mean(), "mean": 100 * r.mean(), "pf": pos / neg if neg > 0 else float("inf"),
            "t": tstat, "days": len(daily)}


def fmt(st):
    if not st:
        return "      (no trades)                      "
    return f"n={st['n']:>5} win={st['win']:5.1f}% mean={st['mean']:+6.3f}% PF={st['pf']:4.2f} t={st['t']:+5.2f}"


def split_time(series, frac=0.6):
    t0 = min(s["t"][0] for s in series.values()); t1 = max(s["t"][-1] for s in series.values())
    return int(t0 + (t1 - t0) * frac)


def run(series, name, cond, direction, sl, rr, hold, ts):
    tr = simulate(series, cond, direction, sl, rr, hold)
    print(f"{name:<58} TRAIN {fmt(stats(tr, ts, 'train'))} | TEST {fmt(stats(tr, ts, 'test'))}")
    return tr


if __name__ == "__main__":
    S = load()
    TS = split_time(S)
    import datetime as dt
    t0 = min(s["t"][0] for s in S.values()); t1 = max(s["t"][-1] for s in S.values())
    print("symbols:", len(S) - 1, "| window:", dt.datetime.utcfromtimestamp(t0 / 1000).date(), "->", dt.datetime.utcfromtimestamp(t1 / 1000).date(),
          "| train/test split:", dt.datetime.utcfromtimestamp(TS / 1000).date())
    fade = lambda s: -np.sign(s["chg24"])
    mom = lambda s: np.sign(s["chg24"])
    band = lambda lo, hi: (lambda s: (np.abs(s["chg24"]) >= lo) & (np.abs(s["chg24"]) < hi))
    print("\n== 1. Current live rule: fade 8-40%, stop 4%, R:R 1.5, max hold 12h")
    run(S, "fade 8-40 sl4 rr1.5 h12 (LIVE)", band(0.08, 0.40), fade, 0.04, 1.5, 12, TS)
    print("\n== 2. Fade vs follow by size of the 24h move (stop 4%, R:R 1.5, hold 24h)")
    for lo, hi in ((0.05, 0.08), (0.08, 0.12), (0.12, 0.20), (0.20, 0.40)):
        run(S, f"fade   {int(lo*100)}-{int(hi*100)}%", band(lo, hi), fade, 0.04, 1.5, 24, TS)
        run(S, f"follow {int(lo*100)}-{int(hi*100)}%", band(lo, hi), mom, 0.04, 1.5, 24, TS)
    print("\n== 3. Same, split by side (fade)")
    up = lambda lo, hi: (lambda s: (s["chg24"] >= lo) & (s["chg24"] < hi))
    dn = lambda lo, hi: (lambda s: (s["chg24"] <= -lo) & (s["chg24"] > -hi))
    for lo, hi in ((0.08, 0.12), (0.12, 0.20), (0.20, 0.40)):
        run(S, f"short the gainers {int(lo*100)}-{int(hi*100)}%", up(lo, hi), fade, 0.04, 1.5, 24, TS)
        run(S, f"buy the losers    {int(lo*100)}-{int(hi*100)}%", dn(lo, hi), fade, 0.04, 1.5, 24, TS)
        run(S, f"buy the gainers   {int(lo*100)}-{int(hi*100)}%", up(lo, hi), mom, 0.04, 1.5, 24, TS)
        run(S, f"short the losers  {int(lo*100)}-{int(hi*100)}%", dn(lo, hi), mom, 0.04, 1.5, 24, TS)
