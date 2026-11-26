# Re-audit fixes — 2026-09-06

Alert writes now merge per ID under an interprocess lock, preserving concurrent additions and rejecting stale changes. Migration and other watchlist writers preserve alerts. Backtest aggregates use pooled trade data; legacy history is normalized on read, and every pane uses real trade rows and net returns. Unknown portfolio drawdown remains unavailable without an equity curve.

Chart quotes are tied to the current symbol/request and expire when their bar is old or fetching fails. Unauthenticated embed messages cannot set the order price. Watchlist and FMP render missing numbers as unknown. Additional fixes cover invalid radar geometry, missing ATR, regular-session prices mislabeled as premarket, DST, small-price precision and exchange-qualified issuer collisions in earnings news.

Validation: isolated full suite 3371 passed, 31 skipped; final issuer-focused run 386 passed; browser checks across 13 tabs and backtest panes; four deterministic quote race/error tests. The opt-in E2E suite was not enabled. Tests used isolated runtime data and no production test orders.

Reproduce focused checks:

```text
python -m pytest tests/test_reaudit_regressions.py
node tests/frontend/test_chart_quote_regressions.cjs
```

The full local evidence report is at C:/Users/<user>/OneDrive/Documente/fix_v9_20260906/REPARATII_2026-09-06.md. This change does not claim that unrelated historical audit backlog or provider rate limits are resolved.
