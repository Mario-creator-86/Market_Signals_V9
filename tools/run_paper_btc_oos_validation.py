"""Run strict price-only OOS research for disabled BTC paper strategies."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.paper_strategy_validation import run_paper_btc_walk_forward
from core.strategy_backtester import _fetch_klines, _to_binance_symbol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="BTC-USD")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=14)
    parser.add_argument("--max-windows", type=int, default=6)
    parser.add_argument("--min-windows", type=int, default=3)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (max(1, args.days) * 24 * 60 * 60 * 1000)
    symbol = _to_binance_symbol(args.ticker)
    bars_1h = _fetch_klines(symbol, "1h", start_ms, end_ms)
    bars_4h = _fetch_klines(symbol, "4h", start_ms, end_ms)
    if bars_1h is None or bars_4h is None:
        print(json.dumps({"status": "source_unavailable", "source": "Binance public OHLCV"}))
        return 2

    reports = [
        run_paper_btc_walk_forward(
            bars_1h,
            bars_4h,
            strategy_id=strategy_id,
            train_days=args.train_days,
            test_days=args.test_days,
            max_windows=args.max_windows,
            min_windows=args.min_windows,
            min_sample=args.min_sample,
        )
        for strategy_id in ("donchian_short_1h", "bollinger_rsi_short_4h")
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticker": args.ticker.upper(),
        "source": "Binance public OHLCV",
        "reports": reports,
    }
    output = args.output or Path("output") / f"paper_btc_oos_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "reports": [
            {"strategy_id": row["strategy_id"], "status": row["status"], "price_validation_status": row["price_validation_status"]}
            for row in reports
        ],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
