# Learning mode: Opus trades movers and news on paper

Approved by the owner on 2026-11-24 ("Mod de învățare, dar cel puțin 10.000 de tranzacții, așa putem învăța!", then "Da, construiește").

## Intent

The paper bot hardly trades. On 2026-11-24 it reviewed 67 candidates and opened none: the rules propose one side only (stocks: buy a stock after a big drop), the AI may only approve or refuse that side, and no news event can start a trade at all. The owner wants the app to act on what it sees, on paper: a sudden move in the scanner (buy or short) backed by news, a big headline (war: gold and oil up, BTC down), with Opus reading the market and opening positions, so that the app learns from many real paper outcomes.

## Goals

- Opus chooses the side (LONG, SHORT or nothing) for a mover, instead of judging a side a rule picked.
- Fresh high-impact news can open trades on the instruments it affects (ETFs for gold, oil, indices, bonds, sectors; the stocks named; crypto).
- Enough trades to learn from: target 10,000 closed learning trades; no automatic stop after 100.
- Every trade is labelled, small, and measured: Opus's hit rate, result by source, by confidence band; a report every 1,000 closed trades.

## Non-goals

- No change to the risk limits (Risk Constitution: positions, exposure, daily loss, risk per trade), the price-integrity check or live mode. Learning mode exists in paper mode only.
- No futures (gold and oil at night are not tradable; crypto stays 24/7).
- Not a claim of an edge: the app's own studies found none for following news sentiment or buying fallen stocks; these trades are the test.

## Anti-goals

- Learning trades must not crowd out or resize the existing experiments (weekly basket, Strategy Lab, dip rule).
- Opus must not be asked the same question twice (same ticker, nothing new), and the mode must pause before it exhausts the daily Opus allowance.

## Approaches considered

1. **Opus as the trader (chosen).** A worker asks Opus about movers and news; Opus's own side and levels become a paper candidate that then goes through the normal pipeline (Risk, price check, Capital). Learns about the AI's judgement, covers shorts and news.
2. Let every rule candidate trade without the AI. Many trades, but nothing learned about the AI, still one side only, no news.
3. Lower the AI's approval threshold on the existing rules. Still one side only and no news path.

## Design

- **Worker** (`core/learning_mode/`), in the engine process next to the AgentManager, every 60 s:
  - Movers: from the scanner snapshot (stocks during the US regular session, crypto 24/7), a move of at least 5%; one ticker per run, a ticker at most once an hour, never one with an open position. Opus receives price, move, volume, the market context, the ticker's recent headlines and the replay evidence, and answers strict JSON: side, confidence, stop %, target %, hold hours, why.
  - News: every 5 minutes, unseen market headlines at most 30 minutes old with impact score at least 35, up to 12 per batch. Opus receives them with the list of instruments tradable now (US hours: GLD, SLV, USO, UNG, SPY, QQQ, IWM, TLT, UUP, sector ETFs, the stocks named, crypto majors; otherwise crypto only) and answers 0 to 3 ideas.
- **Admission**: confidence at least 0.55; stop 1-10%; target at least 1.25 times the stop (the risk gates need 1.2) and at most 4 times; hold 1-72 h. Ideas wait at most 10 minutes for the cycle.
- **Candidates**: strategy types `AI_MOVER_LONG/SHORT` and `AI_NEWS_LONG/SHORT`, source `learning`, admission score 93 (not a probability), carrying Opus's verdict, the headlines, the model and the confidence. They take the normal cycle slot; Risk and the Capital data check stay binding; the pipeline's second AI review is recorded but advisory (Opus already decided).
- **Size**: 1% of equity per position (about 100 $), no per-ticker streak resizing.
- **Exits**: stop, target, or the hold time; the AI exit manager does not close them early.
- **Limits**: at most 100 new learning trades a day; Capital's 100-trade experiment budget does not apply; pause when the process's Claude subscription calls reach the daily limit minus 600.
- **Progress**: `GET /api/learning/state` and a cockpit card: opened, closed of 10,000, net result, win rate, by source and side, by confidence band, latest trades, checkpoint every 1,000.
- **Switch**: `agents.execution.learning_mode.enabled` (on by default in paper).

## Left to the builder

Prompt wording, the exact ETF list, the progress card layout, how the daily count is derived from positions and the trade journal.
