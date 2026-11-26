# Market Structure, Liquidity & Chart Workspace

## Scope

This subsystem replaces indicator-only interpretation with causal OHLCV
annotations. It supplies both the live Strategy V2 context and the historical
replay path, so a strategy cannot access a pivot, pattern, or liquidity pool
before it was observable at the time.

## Components

| Component | Purpose | Safety rule |
| --- | --- | --- |
| `core/market_structure.py` | Confirmed pivots, HH/HL/LH/LL, BOS and CHoCH | A pivot is emitted only after three later candles close. |
| `core/liquidity_engine.py` | Equal-high/low pools and sweep/reclaim events | A wick is insufficient; close must reclaim the liquidity level. |
| `core/chart_patterns.py` | Double top and double bottom | Pattern is only `CONFIRMED` after the neckline is crossed. |
| `core/strategy_v2/evaluators.py` | S7 liquidity sweep reversal candidate | Sweep/reclaim + matching BOS/CHoCH + volume confirmation within five bars. |
| `/api/chart/{symbol}` | Bounded OHLCV, annotations and latest state | Cache-first, timeframe allowlist, same analysis engines as signals. |

## Internal chart

Click the chart icon in the Scanner to open the internal chart workspace. It
renders OHLC candles, confirmed pivot labels, BOS/CHoCH labels, liquidity pool
zones and chart-pattern necklines. Supported timeframes are `1m`, `5m`, `15m`,
`1h`, `4h`, and `1d`.

Example:

```text
GET /api/chart/BTCUSDT?timeframe=5m
```

The endpoint intentionally returns an unavailable payload rather than
fabricating chart data when the venue cannot supply usable OHLCV.

## Signal safety

`S7_LIQUIDITY_SWEEP_REVERSAL` is enabled for crypto, US stocks and configured
commodities as an observable
**research-only** strategy. It can create a visible Signal and a Capital audit
explanation, but is marked non-executable and cannot open a Capital position.
The current configuration still requires profitable, timestamped, faithful
walk-forward validation before Capital eligibility. The blocker is recorded on
every S7 research signal alongside the exact sweep, structure, volume and risk
evidence that produced it.

## Validation

Run the focused quality gate:

```powershell
python -m pytest tests/unit/test_market_structure.py tests/unit/test_liquidity_engine.py tests/unit/test_chart_patterns.py tests/unit/test_liquidity_sweep_evaluator.py tests/test_chart_api.py -q
```

Run the longer research validation outside an interactive console timeout:

```powershell
python tools/run_faithful_v2_technical_validation.py BTC-USD ETH-USD --days 90 --train-days 45 --test-days 7 --max-windows 6 --min-windows 4 --min-sample 30 --output output/structure_liquidity_walk_forward.json
```

Only after a successful out-of-sample report may S7 be removed from
`strategy_v2.research_only_strategy_ids` and considered by the normal Capital
admission path.
