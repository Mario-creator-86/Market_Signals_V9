"""Does Kronos-mini call the direction of crypto over the next hours? A two-year walk-forward test.

Run with the Kronos Trading Terminal's own Python (it has torch and the model code):

    E:\\KronosTradingTerminal\\.venv\\Scripts\\python.exe kronos_backtest.py <out dir> [points=1200]

It uses the terminal's files read-only (official Kronos repo, Kronos-mini and
Kronos-Tokenizer-2k weights) and the terminal's own settings: 512 hourly
candles of context, 20 sampled paths, temperature 1.0, top_p 0.9, and its
LONG / SHORT / WAIT rule (>= 65% of paths on one side and a median move beyond
0.5%). At each sampled hour the model sees only candles that had closed by then;
the forecast is compared with what happened 4, 12 and 24 hours later.

Results are saved after every forecast, so the run can be stopped and resumed.
"""
import datetime as dt
import json
import math
import random
import statistics
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import torch

TERMINAL = Path(r"E:\KronosTradingTerminal")
sys.path.insert(0, str(TERMINAL / "repos" / "Kronos"))
from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402  (official repo code)

OUT = Path(sys.argv[1])
POINTS = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
OUT.mkdir(parents=True, exist_ok=True)
HOUR = 3_600_000
LOOKBACK, PATHS, HORIZONS = 512, 20, (4, 12, 24)
BULL_FRAC = BEAR_FRAC = 0.65
MIN_MOVE_PCT = 0.5
COST = 2 * 0.0004 + 0.00025 + 0.00025
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
         "LTC", "TRX", "NEAR", "UNI", "ATOM", "APT", "ARB", "OP", "SUI", "INJ"]
DAYS = 730


def klines(symbol):
    cache = OUT / f"klines_{symbol}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    end = int(time.time() * 1000) // HOUR * HOUR
    cur, rows = end - DAYS * 24 * HOUR, []
    while cur < end:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&startTime={cur}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "market-research-script"})
        with urllib.request.urlopen(req, timeout=30) as r:
            chunk = json.loads(r.read().decode())
        if not chunk:
            break
        rows += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7])] for k in chunk]
        cur = int(chunk[-1][0]) + HOUR
        time.sleep(0.1)
    rows = [r for r in rows if r[0] + HOUR <= end]          # closed candles only
    cache.write_text(json.dumps(rows))
    return rows


def device():
    if torch.cuda.is_available():
        return "cuda:0", "CUDA"
    try:
        import torch_directml
        if torch_directml.is_available():
            return torch_directml.device(), "DirectML"
    except Exception:  # noqa: BLE001
        pass
    return "cpu", "CPU"


def stats(values):
    n = len(values)
    if n < 8:
        return None
    mean, sd = statistics.fmean(values), statistics.pstdev(values) or 1e-12
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return {"n": n, "mean_pct": mean * 100, "win_pct": sum(v > 0 for v in values) / n * 100,
            "pf": gains / losses if losses else float("inf"), "t": mean / (sd / math.sqrt(n))}


def report(results):
    results = [r for r in results if not r.get("skipped")]
    print(f"\nforecasts: {len(results)} | coins: {len({r['coin'] for r in results})} | "
          f"{min(r['time'] for r in results)[:10]} .. {max(r['time'] for r in results)[:10]}")
    for h in HORIZONS:
        key = f"h{h}"
        rows = [r for r in results if key in r]
        if not rows:
            continue
        moves = np.array([r[key]["median_move_pct"] for r in rows])
        real = np.array([r[key]["realized_pct"] for r in rows])
        hit = float(np.mean(np.sign(moves) == np.sign(real)) * 100)
        up_share = float(np.mean(real > 0) * 100)
        ic = float(pd.Series(moves).rank().corr(pd.Series(real).rank()))     # Spearman, without scipy
        print(f"\nhorizon {h}h: direction of the median path right {hit:.1f}% of the time "
              f"(the market itself was up {up_share:.1f}%); rank correlation predicted vs realized {ic:+.3f}")
        for side, sign in (("LONG", 1), ("SHORT", -1)):
            picked = [r for r in rows if r[key]["signal"] == side]
            s = stats([sign * r[key]["realized_pct"] / 100 - COST for r in picked])
            if not s:
                print(f"  {side:5} signals: {len(picked)} (too few)")
                continue
            quarters = {}
            for r in picked:
                d = dt.datetime.fromisoformat(r["time"])
                quarters.setdefault((d.year, (d.month - 1) // 3), []).append(sign * r[key]["realized_pct"] / 100 - COST)
            judged = [statistics.fmean(v) for v in quarters.values() if len(v) >= 5]
            print(f"  {side:5} signals: n={s['n']:4} ({s['n'] / len(rows) * 100:.0f}% of hours) mean={s['mean_pct']:+.2f}% "
                  f"win={s['win_pct']:.0f}% PF={min(s['pf'], 99):.2f} t={s['t']:+.1f} quarters+ {sum(m > 0 for m in judged)}/{len(judged)}")
        base = stats([r[key]["realized_pct"] / 100 - COST for r in rows])
        if base:
            print(f"  for comparison, LONG at every sampled hour: mean={base['mean_pct']:+.2f}% win={base['win_pct']:.0f}%")


def main():
    data = {}
    for coin in COINS:
        try:
            rows = klines(coin + "USDT")
        except Exception as exc:  # noqa: BLE001
            print(f"{coin}: no data ({exc.__class__.__name__})")
            continue
        if len(rows) > LOOKBACK + 24 * 30:
            data[coin] = rows
    print(f"coins with history: {len(data)}")

    rng = random.Random(20261123)
    candidates = []
    for coin, rows in data.items():
        for i in range(LOOKBACK, len(rows) - max(HORIZONS)):
            candidates.append((coin, i))
    points = rng.sample(candidates, min(POINTS, len(candidates)))
    points.sort(key=lambda p: (p[0], p[1]))

    results_path = OUT / "kronos_results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    done = {(r["coin"], r["index"]) for r in results}
    todo = [p for p in points if p not in done]
    print(f"forecasts planned: {len(points)}, already done: {len(points) - len(todo)}")
    def load(force_cpu=False):
        dev, label = ("cpu", "CPU") if force_cpu else device()
        tokenizer = KronosTokenizer.from_pretrained(str(TERMINAL / "models" / "Kronos-Tokenizer-2k")).eval()
        model = Kronos.from_pretrained(str(TERMINAL / "models" / "Kronos-mini")).eval()
        print(f"model loaded on {label}", flush=True)
        return KronosPredictor(model, tokenizer, device=dev, max_context=2048)

    predictor = load() if todo else None
    failures_in_a_row, on_cpu = 0, False
    started = time.time()
    for n, (coin, i) in enumerate(todo):
        rows = data[coin]
        window = rows[i - LOOKBACK:i]                          # candles closed before the decision hour
        frame = pd.DataFrame(window, columns=["ts", "open", "high", "low", "close", "volume", "amount"])
        x_ts = pd.Series(pd.to_datetime(frame["ts"], unit="ms"))
        base_ts = int(window[-1][0])
        y_ts = pd.Series(pd.to_datetime([base_ts + (k + 1) * HOUR for k in range(max(HORIZONS))], unit="ms"))
        x_df = frame[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        torch.manual_seed((hash((coin, i)) & 0x7FFFFFFF))
        try:
            preds = predictor.predict_batch(df_list=[x_df] * PATHS, x_timestamp_list=[x_ts] * PATHS,
                                            y_timestamp_list=[y_ts] * PATHS, pred_len=max(HORIZONS),
                                            T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False)
        except Exception as exc:  # noqa: BLE001
            # One bad forecast on DirectML leaves the device unusable for the rest of the
            # process: skip this point, start again with fresh weights, and move to the CPU
            # if the GPU keeps failing.
            print(f"  forecast failed for {coin} at {i}: {exc.__class__.__name__}: {exc}", flush=True)
            results.append({"coin": coin, "index": i, "skipped": True})
            results_path.write_text(json.dumps(results))
            # Even fresh weights cannot be loaded on the broken device in this process:
            # leave with a code that tells the caller to start the script again.
            sys.exit(75)
        closes = np.stack([p["close"].values for p in preds])
        if not np.isfinite(closes).all():
            continue
        base = float(window[-1][4])
        item = {"coin": coin, "index": i, "time": dt.datetime.fromtimestamp((base_ts + HOUR) / 1000, dt.timezone.utc).isoformat()}
        for h in HORIZONS:
            finals = closes[:, h - 1]
            bull, bear = float((finals > base).mean()), float((finals < base).mean())
            move = (float(np.median(finals)) - base) / base * 100
            signal = "LONG" if bull >= BULL_FRAC and move > MIN_MOVE_PCT else \
                "SHORT" if bear >= BEAR_FRAC and move < -MIN_MOVE_PCT else "WAIT"
            realized = (rows[i + h - 1][4] / base - 1) * 100
            item[f"h{h}"] = {"signal": signal, "bullish_frac": bull, "median_move_pct": move, "realized_pct": realized}
        results.append(item)
        if (n + 1) % 10 == 0 or n + 1 == len(todo):
            results_path.write_text(json.dumps(results))
        if (n + 1) % 50 == 0:
            rate = (time.time() - started) / (n + 1)
            print(f"  {n + 1}/{len(todo)} forecasts, {rate:.1f}s each, about {(len(todo) - n - 1) * rate / 60:.0f} min left", flush=True)
    if results:
        report(results)


if __name__ == "__main__":
    main()
