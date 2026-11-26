# V8 Strict/Custom Variant Upgrade Summary
Date: 2026-02-08

## Summary
- Added dual variant modes: STRICT (default) and CUSTOM (user-configurable), with STRICT priority.
- Implemented strict priority rules:
  - CUSTOM ignored if STRICT already open.
  - STRICT always replaces any existing signal (same/opposite direction), skipping cooldown.
  - Replacement info is saved in `replaced_signal` and surfaced in UI.
- Added strict backing fields to signals and persisted them in signals_state.json.
- Propagated variant_mode and strict_backed info across engine, API, UI, and adapters.
- Extended backtest flow to accept variant_mode for comparisons, standard runs, and walk-forward.

## Backward Compatibility
- Missing variant_mode defaults to STRICT in UI and stats.
- Existing signals without new fields load correctly.

## Files Modified
- core/engine.py
- core/signals.py
- core/quick_backtester.py
- core/ui_signals_adapter.py
- core/ui_state_adapter.py
- api/main.py
- api/actions_service.py
- api/state_service.py
- frontend/app.js
- frontend/index.html
- docs/TRADING_STRATEGIES.md
- WINRATE_CHANGES.md
- tests/test_v8_signal_features.py

## Tests Run
- pytest -q tests/test_v8_signal_features.py

## Notes
- New signal fields include: variant_mode, strict_backed, strict_backed_at, strict_variant,
  strict_risk_mode, strict_signal_mode, strict_signal_score, strict_probability_score,
  strict_mtf_alignment.
- Backtest requests now support variant_mode=STRICT|CUSTOM from UI and API.
