# Learning Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opus opens small paper trades on scanner movers (choosing LONG or SHORT) and on fresh high-impact news, toward 10,000 measured trades.

**Architecture:** A worker thread in the engine process (`core/learning_mode/`) asks Opus about one mover per minute and about new headlines every 5 minutes, and queues admitted ideas; the AgentManager's autonomous cycle takes them as a new candidate source (`learning`) through the unchanged Risk, price-integrity and Capital gates. A ledger under `data/learning_mode/` records every closed learning trade so progress survives the 300-position limit of the capital state.

**Tech Stack:** Python 3.14 standard library, existing app modules (ollama client with Claude subscription routing, AgentManager, Capital manager, news tab agent), vanilla JS cockpit.

**Spec:** `docs/superpowers/specs/2026-11-24-learning-mode-design.md`

## Global Constraints

- Paper mode only; live mode: no learning candidates, AI veto unchanged.
- Admission: confidence >= 0.55; stop 1-10 %; target >= 1.25 x stop and <= 4 x stop; hold 1-72 h; idea waits at most 10 min.
- Size 1 % of equity per position; at most 100 new learning trades per local day; target 10,000 closed trades, checkpoint every 1,000.
- Movers: |move| >= 5 %, stocks only in the US regular session, crypto 24/7; a ticker at most once per 60 min; no ticker with an open position.
- News: every 300 s; headlines <= 30 min old, impact score >= 35, unseen, at most 12 per batch, at most 3 ideas.
- Pause when the process's Claude subscription calls today >= daily limit - 600.
- Strategy types `AI_MOVER_LONG/SHORT`, `AI_NEWS_LONG/SHORT`; `autonomous_source` `learning`; `signal_mode` `AUTONOMOUS_LEARNING`; admission score 95 (ordering only, not a probability).
- Risk agent, Risk Constitution, price integrity, Capital data check stay binding; the pipeline AI review is advisory for learning signals; no Capital 100-trade budget; no AI exit management; no per-ticker streak resizing.
- Settings under `agents.execution.learning_mode`; `enabled: true` by default.

## Review Focus

- Opus answers with prose around the JSON, or a side/ticker outside the allowed set: the idea is dropped, nothing crashes (Task 2 tests).
- The same headline seen again on the next scan, or a headline whose clock is in the future: asked once, never re-asked (Task 3 test).
- An idea that waits past 10 minutes (cycle busy): it is dropped, not traded at a stale price (Task 3 test).
- The worker raises (scanner down, news agent missing, Opus error): the cycle and the other sources keep running (Task 4 test).
- The app restarts: daily count and the closed-trade ledger are read back, no trade counted twice (Task 3 test).

---

### Task 1: Settings, identity and advisory rule

**Files:**
- Create: `core/learning_mode/__init__.py`, `core/learning_mode/settings.py`
- Modify: `core/agent_manager.py` (`_ai_block_is_advisory`), `core/capital_entry_guard.py` (`_paper_autonomous_experiment_source`, budget exemption), `core/capital_manager.py` (`_ai_exit_management_enabled`), `agents/execution_agent.py` (`basket_ticker_adjustment`, sizing), `core/config.py` (defaults)
- Test: `tests/unit/test_learning_mode_settings.py`

**Interfaces:**
- Produces: `learning_config(exec_cfg: dict) -> dict`; `is_learning_signal(signal: Any) -> bool`; `learning_ai_is_advisory(signal: Any, exec_cfg: Any) -> bool`; constants `LEARNING_SOURCE = "learning"`, `MOVER_PREFIX = "AI_MOVER_"`, `NEWS_PREFIX = "AI_NEWS_"`, `MACRO_INSTRUMENTS: tuple[str, ...]`.
- Capital class `"learning"`: admitted as paper experiment, no `evaluate_paper_experiment_budget`.

- [ ] Step 1: tests - defaults equal the Global Constraints values; `is_learning_signal` true only for source `learning` plus an `AI_MOVER_`/`AI_NEWS_` strategy type; advisory true in paper, false in live and for `SCANNER_AI_LONG`; `_advisory_override_applies` keeps `ai_validation_policy` binding; Capital entry of a learning signal with valid levels returns `paper_capital_experiment: True` and no `paper_experiment_budget` check; `_ai_exit_management_enabled` false for `AI_NEWS_LONG`; `basket_ticker_adjustment` sets aside the adjustment for a learning signal.
- [ ] Step 2: run, expect failures (module missing).
- [ ] Step 3: implement; execution sizing: for a learning signal with `learning_size_usd > 0`, `invested_amount = min(learning_size_usd, balance * base_pct)` and size factor 1.0.
- [ ] Step 4: run the new tests and `tests/unit/test_rule_basket_safety.py`, expect pass.
- [ ] Step 5: commit.

### Task 2: Opus trader calls

**Files:**
- Create: `core/learning_mode/trader.py`
- Test: `tests/unit/test_learning_mode_trader.py`

**Interfaces:**
- Consumes: `learning_config`, `MACRO_INSTRUMENTS`.
- Produces: `admit_idea(raw: dict, cfg: dict) -> Optional[dict]` returning `{"side": "LONG"|"SHORT", "confidence": float, "stop_pct": float, "target_pct": float, "hold_hours": int, "why": str}`; `ask_mover(row: dict, context: dict, cfg: dict, *, generate=None, model=None) -> dict` returning `{"idea": Optional[dict], "model": str, "error": str, "answer": dict}`; `ask_news(headlines: list[dict], tradable: list[str], context: dict, cfg: dict, *, generate=None, model=None) -> dict` returning `{"ideas": list[dict], "model": str, "error": str}` where each idea also has `ticker`.

- [ ] Step 1: tests - a JSON answer inside prose is parsed; side NONE or confidence 0.54 gives no idea; stop 0.5 % clamps to 1 %, target below 1.25 x stop is raised to 1.25 x stop, hold 200 h clamps to 72; a news idea on a ticker outside `tradable` is dropped; at most 3 news ideas; generate error gives `error` and no idea; the prompt names the session and the tradable list, and says a paper learning trade is wanted when Opus has a view.
- [ ] Step 2: run, expect failures.
- [ ] Step 3: implement; model from `assigned_model("market_analyst"| "news_analyst", fallback=ollama.get_best_model())`, timeout `call_timeout_for(model, 90)`.
- [ ] Step 4: run, expect pass.
- [ ] Step 5: commit.

### Task 3: Book of ideas, signals and ledger

**Files:**
- Create: `core/learning_mode/book.py`
- Test: `tests/unit/test_learning_mode_book.py`

**Interfaces:**
- Consumes: Task 2 idea dict.
- Produces: `class LearningBook(root: Path)` with `add_idea(idea: dict, *, ticker: str, source: str, price: float, asset_class: str, headlines: list, model: str, now: datetime) -> dict`; `candidates(exec_cfg: dict, now: datetime) -> list[dict]` (unexpired, returns signals once); `may_ask_mover(ticker: str, now) -> bool`, `note_mover_asked(ticker, now)`; `unseen_headlines(rows: list[dict], now) -> list[dict]`, `note_headlines_seen(rows)`; `opened_today(positions, now) -> int`; `sync_closed(positions) -> int` (appends to `closed.jsonl`, returns how many new); `progress(cfg) -> dict`. `build_learning_signal(entry: dict) -> dict`.

- [ ] Step 1: tests - signal fields (strategy type, source, mode, levels from price and percents, `time_stop_minutes`, `learning_size_usd` absent until sized, `score` 95, `learning` block); an idea older than 10 min is not returned; a headline id is offered once and survives a reload of the book; `sync_closed` counts a position once across two calls and a reload; `progress` gives closed, net, win rate, by source, by side, by confidence band (0.55-0.65, 0.65-0.75, 0.75+), checkpoint = closed // 1000.
- [ ] Step 2: run, expect failures.
- [ ] Step 3: implement; state in `data/learning_mode/state.json` (atomic replace), ledger `closed.jsonl`.
- [ ] Step 4: run, expect pass.
- [ ] Step 5: commit.

### Task 4: Worker

**Files:**
- Create: `core/learning_mode/worker.py`
- Test: `tests/unit/test_learning_mode_worker.py`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `class LearningWorker(*, book, config: Callable[[], dict], scanner_state: Callable[[], dict], news_rows: Callable[[], list], positions: Callable[[], list], quote: Callable[[str, str], float], ask_mover=..., ask_news=..., usage=..., clock=...)` with `run_once() -> dict` (what it did) and `start()`/`stop()`; `get_learning_worker(manager) -> LearningWorker` singleton.

- [ ] Step 1: tests - live mode, disabled, daily cap reached, or Opus budget low: nothing asked; picks the biggest unasked mover, skips a stock outside the session and a ticker with an open position; an admitted answer lands in the book; a news scan happens only after 300 s and only with unseen qualifying headlines; at night the tradable list has crypto only; any callable raising is reported in the result, not raised.
- [ ] Step 2: run, expect failures.
- [ ] Step 3: implement; session check with `core.market_hours` helpers the scanner already uses.
- [ ] Step 4: run, expect pass.
- [ ] Step 5: commit.

### Task 5: Wire into the autonomous cycle

**Files:**
- Modify: `core/agent_manager.py` (`_learning_candidates`, `_autonomous_monitor_once`, `_drop_concluded_experiments`)
- Test: `tests/unit/test_learning_mode_cycle.py`

**Interfaces:**
- Consumes: `get_learning_worker`, `LearningBook.candidates`.
- Produces: source `learning` in `_autonomous_source_stats`; `autonomous_sources.learning` switch.

- [ ] Step 1: tests - the cycle includes learning candidates with source stats; a disabled source adds none; size of a learning signal is 1 % of equity (`learning_size_usd`); concluded-experiment filter keeps learning candidates.
- [ ] Step 2: run, expect failures. Step 3: implement. Step 4: run, expect pass. Step 5: commit.

### Task 6: Progress API and cockpit card

**Files:**
- Modify: `api/main.py` (`GET /api/learning/state`), `frontend/cockpit.js` (card), `frontend/index.html` (version bump)
- Test: `tests/unit/test_learning_mode_api.py`, `tests/frontend/learning_mode_card.cjs`

- [ ] Step 1: tests - endpoint returns `progress` with `target 10000`, `closed`, `opened_today`, `daily_cap 100`, `paused_reason`, `recent`; the card renders "closed of 10,000", net result, win rate, by source and band, and escapes ticker text.
- [ ] Step 2-5: fail, implement, pass, commit.

### Task 7: Verify live

- [ ] Full suite `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider --timeout=180` and the 24+ frontend tests pass.
- [ ] Restart the app; within 30 minutes the trade audit shows `AI_MOVER_*` or `AI_NEWS_*` candidates and at least one `position_opened`; `/api/learning/state` reports them.
- [ ] Commit; report to the owner.
