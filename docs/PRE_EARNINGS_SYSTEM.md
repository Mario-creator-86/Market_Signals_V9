# PRE-EARNINGS System

## Scope
Pre-Earnings adds a cache-first scanner focused on earnings setup risk and directional bias.

- UI tab: `PRE-EARNINGS`
- Agent: `PreEarningsAgent` (ON/OFF from AGENTS tab)
- Persisted output: `data/pre_earnings.json`
- Durable per-event AI registry: `data/pre_earnings_ai_cache.json`
- Durable Pulse registry: `data/pre_earnings_pulse_history.json` (under
  `MS_DATA_DIR`, or `PRE_EARNINGS_PULSE_HISTORY_PATH` when explicitly
  overridden)
- Internal API:
  - `GET /api/pre-earnings`
  - `POST /api/pre-earnings/refresh`
  - `POST /api/pre-earnings/refresh-symbol?symbol=`
  - `POST /api/pre-earnings/coverage/warmup` + `GET /api/pre-earnings/coverage/warmup` (status)

## Absolute Cache-First Rules

1. News
- Source: existing `NewsTabAgent` cache.
- **Note (deviation from original spec):** when symbols have missing news
  coverage or enter the priority shortlist, the agent performs a bounded
  per-ticker deep refresh in a background thread. Six priority names are
  refreshed first and the remaining slots rotate through the full earnings
  slate, so a 200-name calendar cannot starve while the best candidates remain
  fresh. The current cycle waits at most 10 seconds and otherwise serves cache;
  the next cycle consumes the persisted background results.

2. Earnings Calendar
- Source: existing `EconomicCalendarAgent` cache (FinnHub bulk).
- yfinance fallback for past-earnings dates when the calendar lacks them.
- External refresh allowed only if cache missing/stale and agent is ON.
- No duplicate calendar fetcher.

3. Other Data
- Uses local cached data only when present (quote/fundamental/history snapshots).
- Real yfinance analyst data (target-price upside, recommendation mean, count)
  and post-earnings reaction history are fetched and contribute to DirectionBias.
- Missing components stay `None` and are excluded from the weighted average
  (never faked to neutral); `data_quality_flag = SUSPECT_INSUFFICIENT_DATA`
  when ≥2 components are missing.
- Scanner remains deterministic and explainable.

4. UI
- Reads only internal API.
- Never triggers external market/news requests.

## Core Scores

### A) GapRisk (0..100)
Represents probability of large earnings gap.
Computed via weighted average over available contributors:

- `historical_move` (weight 0.25)
- `news_intensity` (weight 0.10)
- `catalyst_severity` (weight 0.20)
- `liquidity_risk` (weight 0.10)
- `implied_move` (weight 0.35, only when available)

If `implied_move` is unavailable, weights are re-normalized over available
contributors. Weights are eligible for quarterly auto-recalibration from local
post-earnings outcomes.

Interpretation:
- `0-39`: low gap-risk regime
- `40-69`: medium regime
- `70-100`: high gap-risk regime

### B) DirectionBias (-100..+100)
Represents directional tilt, not certainty.

- Negative values: short tilt
- Positive values: long tilt
- Around 0: mixed/unclear
- `direction_label`: `LONG` (>= +15), `SHORT` (<= -15), else `NEUTRAL`

Contributors (5 components, weights are *relative* — the average divides by
available weight so only the ratios matter):

- `c1_historical` — historical beat rate (weight 0.25). The raw rate is
  damped by sample support before it contributes: `support = min(1,
  samples/4)`, so `c1 = 50 + (beat_rate*100 - 50) * support`. A single 1/1
  quarter is a coin flip (contributes 62.5, quarter-strength), not a fake
  100% beat rate; full strength requires ≥4 completed quarters. The raw rate
  and sample count stay visible via `beat_rate_historical` /
  `beat_rate_samples`.
- `c2_analyst_revision` — analyst consensus: target-price upside blended with
  recommendation mean (weight **0.30**, the strongest predictor — real yfinance
  analyst data). Restored to 0.30 from a previous 0.28 that contradicted its
  own "(30%)" spec comment.
- `c3_options` — options calls/puts ratio (weight 0.25)
- `c4_news` — news AI sentiment, always computable (weight 0.20)
- `c5_history` — post-earnings reaction tilt, a small nudge (weight 0.12)

Components that are `None` (no data) are excluded and weights re-normalized.

NEWS_ONLY_CALL guard: when `gap_risk_unavailable` is true AND fewer than 2
non-news direction components (c1/c2/c3/c5) are available, the only real
evidence is headline sentiment. The row's `direction_label` is clamped to
`NEUTRAL` (the score stays computed for transparency), `news_only_call=true`
is emitted, and the row carries the `NEWS_ONLY_CALL` badge instead of a
`LONG_BIAS`/`SHORT_BIAS` badge. An explicit AI verdict may still break the
tie in the final blend (documented AI participation); the badge persists.

## AI / LLM Participation in the Decision (long/short + score)

The LLM (Ollama cloud — local is skipped as too slow for usable latency) is a
**first-class participant** in the long/short call and the score, not just a
sentiment scalar:

- For the highest-ranked not-yet-reviewed or stale tickers (configurable
  `ollama_max_tickers`, default 25), the agent calls the LLM over up to 12
  deduplicated, source-diverse news records including timestamps, sources and
  bounded summary text. Selection prioritizes likely Top-10 setups by coverage,
  opportunity, confidence and directional strength rather than GapRisk alone.
- The LLM returns: `sentiment` (-1..+1), an explicit `decision`
  (`LONG`/`SHORT`/`NEUTRAL`), `decision_confidence` (0..1),
  `decision_rationale`, `catalyst_label`, plus `why_bullets`/`what_to_watch`.
- Each enriched item gets an `ai_decision` block:
  `{used, verdict, confidence, sentiment, rationale, catalyst_label, model}`.
- The verdict+sentiment are **blended** into `direction_bias_score` with a
  **configurable weight** (`PRE_EARNINGS_OLLAMA_DECISION_WEIGHT`, default 0.35,
  clamped 0–0.6):

      new_score = (1 - w) * structured_score + w * (sentiment*100 + verdict_nudge)

  where `verdict_nudge = ±10 * decision_confidence` (LONG positive, SHORT
  negative). The AI is a real contributor but **cannot, on headlines alone,
  override** a data-driven call (the previous hard-coded 0.6 could). A
  confident AI verdict breaks a NEUTRAL tie.
- The UI keeps three concepts visually distinct: `Structured`, `LLM View`, and
  `Final Blended Signal`. Scores are signed values in `-100..+100`; only LLM
  confidence is a percentage. This prevents a final `NEUTRAL +13` from being
  misread as "13% confidence" or contradicted by an `LLM SHORT, 75% confidence`
  input.
- When the LLM is unavailable (no cloud key, circuit open, ticker outside the
  batch), no fake AI signal is emitted — the structured data-driven call
  stands and the UI shows an honest "not available" note.
- Every AI verdict records the evidence fingerprint, article IDs, source count
  and newest article time. If a previously unseen article was published after
  `ai_decision.generated_at`, the verdict remains visible but is marked stale,
  re-enters the bounded AI queue and receives
  `refresh_reason="news_changed"` after successful re-analysis.

The tab also exposes one aggregate **Pre-Earnings Pulse** over the 20
highest trader-ranked items. It is cloud-only, non-fatal, cached for 300 seconds by
default, and returns `summary`, `sentiment`, `top_long`, `top_short`,
`catalysts`, and `risks`. Per-ticker analysis remains independent. The prompt
anchors each candidate with its current final label+score (`LONG+43`), and
every pick is post-validated against the live rows: a pick whose side opposes
the same row's current `direction_label` is dropped — at generation, on the
merge/import path, and during active-union reconciliation (archived as
`direction_contradiction`) — so the card never contradicts the item row.
NEUTRAL rows are never candidates for either side. The response exposes both
the durable event history (`top_long` / `top_short`) and the exact current
snapshot (`current_top_long` / `current_top_short`); the live card renders only
the latter, so an older same-event pick cannot masquerade as today's choice.
The Pulse input fingerprint includes current scores, event dates, AI verdicts
and news fingerprints; materially changed inputs bypass the still-live TTL so
new evidence can update the pulse immediately.

## Daily Top 10 Trader Ranking

`GET /api/pre-earnings` includes `top_predictions` (at most 10) and a `ranking`
audit block. This is a two-stage pipeline: every canonical calendar event is
light-screened, then at most `max_results` nearest/strongest candidates receive
the expensive news, fundamentals and AI scoring. The audit block reports both
`light_scanned_count` and `deep_scored_count`; it never claims that thousands
of events were deeply analyzed. Eligibility requires `event_status=UPCOMING`,
verified date/session provenance, no provider date conflict, a LONG/SHORT final
signal, an aligned non-NEUTRAL AI verdict, minimum verified coverage, minimum
directional magnitude, a real GapRisk sample, at least three relevant articles
from at least two actual publishers, and news no older than 96 hours. A startup
or legacy row without canonical event metadata fails closed and remains visible
only for audit. The evidence-weighted `trader_score` combines direction strength,
coverage, confidence, GapRisk, AI alignment, shrinkage-adjusted per-ticker track
record, news breadth/source diversity and event proximity. It penalizes AI/data
conflicts, calendar/news mismatches, missing news and a pending AI refresh.

The score is a **relative conviction/ranking score, not a win probability**.
The UI states this explicitly and shows the input counts used for the scan. AI-
review is required by default. The result may contain fewer than 10 names;
the service never fills empty ranks with weak or unverifiable calls. If the
last successful snapshot exceeds
`recommendation_max_age_seconds` (default 30 minutes), the API suppresses the
shortlist and the UI displays a stale warning while retaining raw rows for
audit. The same guard applies to cache-only disk reads, preventing a server
restart from presenting an old list as today's recommendations.

### Top 10 History

`GET /api/pre-earnings/track-record` exposes a separate Top-10 audit population:
`top_predictions_history` contains matured recommendations and
`top_predictions_pending` contains recommendations still awaiting the post-
earnings evaluation window. When an event first enters Top 10, the system locks
its LONG/SHORT direction, direction bias, Trader Score, score band, entry rank,
best rank and first-seen timestamp. This provenance is monotonic: a later score,
direction or eligibility refresh cannot erase or rewrite what the trader saw.

Top-10 accuracy is computed only from this population and only against the
captured Top-10 direction. A result matures approximately 36 hours after the
earnings event. The UI reports the realized signed move plus HIT/MISS, with
separate overall, LONG and SHORT hit rates. Zero/non-finite moves remain visible
as settled but unscored; they do not become artificial misses. Rows created
before entry snapshots existed are explicitly marked `legacy_reconstructed`,
and no historical success rate is fabricated for events that were never
provably displayed in Top 10.

### Durable per-event AI decisions

Every valid `ai_decision.used=true` result is also written atomically to
`pre_earnings_ai_cache.json`. The sidecar is independent of the currently
visible top-N rows, so a later partial scan, provider miss, page reload, or
process restart cannot erase an analysis that already completed successfully.

Entries are keyed by normalized symbol plus earnings event time and are reused
only for the same event window (maximum 14-day date drift). A new quarterly
event cannot inherit the previous quarter's verdict. When a fresh structured
row is rebuilt, the saved AI decision and its Ollama provenance are restored,
then the final blended direction is recomputed from the current structured
inputs. The bounded journal retains the newest 1,200 valid analyses.

Prediction feedback is monotonic for AI provenance: registering the same open
prediction without a new AI result does not replace a previously saved valid
AI verdict with `null`. A historical PLTR AI completion could not be proven
from the pre-fix logs or feedback snapshot; the sidecar guarantees this
durability for successful analyses from this implementation onward.

## Peer read-through & AI re-analysis

Owner requirement (2026-11): "Yesterday's events should populate history
grouped by sector and by day... if HUT and CIFR report below expectations and
the decision was LONG, then for MARA, CLSK and RIOT it must RE-ANALYZE!"

**Peer grouping key.** Every built item carries `peer_key` / `peer_key_label`
resolved through a chain: curated theme override (`PEER_THEME_OVERRIDES`, e.g.
HUT/CIFR/MARA/CLSK/RIOT/WULF/IREN -> `BTC_MINERS` — a pure sector join would
split CIFR "Technology" from the other miners) -> `industry` (normalized
UPPER_SNAKE, from the same 6h analyst-estimates yfinance `.info` payload that
already resolves `sector`, so no extra provider request) -> `sector`. A name
whose whole chain is UNKNOWN/blank resolves to `null` and groups with NOTHING
— never with a synthetic "UNKNOWN" pool. Operators can extend the map via
`pre_earnings.peer_theme_overrides` (symbol -> theme), merged over defaults.

**Read-through evidence.** Each item's `peer_readthrough` summarizes settled
outcomes in the same peer group whose evaluation landed within the last
`peer_readthrough_hours` (default 96h, clamped 24-336), newest first, capped
at `peer_readthrough_max_peers` (default 5): per-peer signed move + AI
verdict, count, average signed move, positive share, and a `verdict`
(bearish at <=34% positive, bullish at >=67%, else mixed; a single peer never
tips it). `peer_readthrough` is `null` when no fresh settled peers exist —
honest, never an empty-object fake. The key/label are persisted on open
predictions and echoed onto settled outcomes (legacy UNKNOWN predictions are
re-resolved at settlement through the same chain, budget-respecting), and the
track-record endpoint exposes `sector`, `peer_key_label` and `event_day` per
row so history groups by sector/theme and by day client-side.

**AI prompt + re-analysis staleness.** When a read-through exists, the AI
prompt gains one compact line (`HUT -12.4%, CIFR -8.1% (2 peers, 0% positive)
— bearish read-through`) and the system prompt instructs the model to treat
peer results as the freshest hard evidence. A saved per-event verdict whose
`generated_at` is OLDER than the item's current
`peer_readthrough.latest_evaluation_utc` is stale
(`_ai_verdict_stale_by_peer_evidence`): the name re-enters the normal
per-cycle enrichment pool in both reuse paths (`_carry_forward_ai_decision`
marks the reuse `stale_by_peer_evidence`; `_apply_ollama`/the early AI pass
keep stale items eligible under the same highest-GapRisk-first ordering and
the same `ollama_max_tickers` quota — re-analysis is bounded, never
unbounded). A refreshed verdict carries `refresh_reason="peer_readthrough"`.
Honesty rules: staleness only UNLOCKS re-enrichment — when the LLM is
unavailable the previous verdict keeps being served (never dropped to null),
and unprovable ordering (missing timestamps) is honestly "not stale" so a
legacy verdict cannot burn LLM budget every cycle.

## Implemented Accuracy Upgrades

1. Strict historical move computation
- Historical move uses cached earnings event windows + cached OHLC **daily**
  bars (`_load_symbol_daily_bars`). The previous 4H-bar approach was replaced
  with true daily open/close for tighter event-window precision.
- `_compute_event_gap_move_pct` guards against `NaN`/`Inf` reference prices
  (yfinance can deliver NaN for illiquid tickers) to keep the persisted JSON
  store RFC-8259-valid.

2. Sector/MarketCap calibration
- GapRisk is adjusted by learned calibration factors (`sector x cap bucket`)
  from local outcomes.
- `sector` first uses the state/fundamental snapshot, then falls back to the
  existing six-hour analyst-estimates cache. That cache surfaces
  `sector`/`industry` from the same yfinance `.info` response, so no extra
  provider request is introduced.

3. Quarterly feedback retuning
- Contributor weights can be retuned once per quarter using local realized
  outcomes (`last_recalibration_quarter`).
- **Not validated end-to-end by tests:** the automatic retuning mechanism
  exists but no test asserts that accumulated outcomes change
  `weight_overrides` for the next cycle.

4. Live calibration metrics
- Payload includes:
  - sample size
  - bucket hit-rates
  - Brier-like score
  - MAE
  - directional accuracy for stronger bias calls
- Aggregations filter non-finite realized moves so already-corrupted stores
  self-heal on the next run.

5. Confidence intervals
- Each ticker includes confidence intervals for GapRisk and DirectionBias
  (`_estimate_confidence_intervals`).
- Intervals shrink/expand based on data coverage and calibration sample support.
- **Not validated by tests** (interval width vs sample count).

## Calendar Mismatch Detection

Scanner flags `POSSIBLE_MISSING_EARNINGS_EVENT` when:
- cached news repeatedly signals earnings context (e.g. "reports Q", "earnings",
  "EPS", "results"),
- but no earnings event exists within `lookahead_days` (configurable, default 7)
  for that symbol, AND a recent lookback window (`recent_hours`) has no event.

UI badge: `CALENDAR_MISMATCH`

This prevents silent misses when calendar providers lag or are incomplete.
Detection requires ≥2 news hits + future earnings present + no nearby event,
then prunes against cached history to avoid false positives.

## Past-Date Filtering

- Filtering is session-aware because many calendar rows are date-only and
  parse as midnight UTC. A same-day BMO event remains upcoming until 14:30
  UTC; AMC remains upcoming until 22:00 UTC. Past dates are always reported,
  future dates are always upcoming.
- A same-day row with an unknown session uses its exact time when one is
  supplied; date-only unknown-session rows and undated/malformed rows stay in
  the radar rather than being silently discarded.
- Removed rows are exposed in top-level `reported_today` transparency records
  instead of disappearing from the API/UI.

### News-confirmed already-reported events

Calendar dates can lag reality, so recent post-earnings headlines are also used
to set `flags.already_reported=true` with
`flags.already_reported_evidence`. A news-confirmed row is excluded from normal
per-ticker LLM enrichment and from the aggregate Pulse prompt. If every
candidate is news-confirmed as already reported, Pulse skips the LLM call
entirely rather than producing a forward-looking recommendation for a
completed event.

This flag also wins over a stale Pulse recommendation: the same ticker/event
cannot remain in the active LONG/SHORT union merely because the calendar still
shows a future date or an imported historical snapshot contains it.

## Output Contract

Each item (`_build_item_for_symbol`) emits ~35 fields:
- `symbol`, `earnings_time_utc`, `session`, `confirmed`
- `gap_risk`, `gap_risk_level`, `gap_risk_numeric`,
  `gap_risk_unavailable`, `gap_risk_unavailable_reason`, `gap_risk_history_source`
- `direction_bias`, `direction_bias_score`, `direction_label`
- `structured_direction_bias_score`, `structured_direction_label`,
  `llm_direction_score`, `final_direction_bias_score`, `decision_alignment`
- `data_confidence`, `data_message`
- `components`, `direction_components`
- `analyst_target_upside_pct`, `analyst_recommendation`, `analyst_rec_mean`,
  `analyst_count`
- `recent_reactions`, `symbol_track_record`, `track_record_overall`
- `beat_rate_historical`, `beat_rate_beats`, `beat_rate_samples`,
  `beat_rate_source`, `beat_rate_unavailable_reason`
- `expected_move_pct`, `historical_median_move_pct`, `avg_move_historical`,
  `historical_sample_size`, `range_proxy_pct`
- `sector`, `market_cap`, `market_cap_bucket`
- `coverage_score`, `coverage_label`, `coverage_contributors`, `opportunity_score`
- `badges`, `why`, `what_to_watch`, `bias_reasons`, `top_news`
- `contributors`, `confidence`, `confidence_score`
- `ai_sentiment`, `ai_decision`, `ollama` (when the LLM ran)

Top-level payload also includes `as_of_utc`, `lookahead_days`,
`refresh_seconds`, `inputs_used`, `status` (with `last_run_time`,
`last_success_time`, `last_error`, `next_run_in`, `last_recalibration_quarter`,
`coverage_warmup`), `disabled`, and `calibration` metrics. Premium additions:
- `pulse`: object or `null`, with `summary`, `sentiment`, `top_long`,
  `top_short`, `catalysts`, `risks`, `model`, and `generated_at`. The two top
  arrays are the cumulative union of still-upcoming ticker/event picks, not
  only the latest model top three. `history` exposes the newest 20 material
  snapshots by default, newest first; `history_meta` exposes unique snapshot,
  generation, archive, and active-pick counts.
- `reported_today`: array of `{symbol, earnings_time_utc, session, gap_risk,
  direction_label, is_watchlist}`.
- `flags.pulse_used` and `flags.reported_today_count`.
- `flags.ollama_used` means **fresh LLM enrichment ran in this cycle**;
  verdicts restored from the durable per-event registry do not set it.
  `flags.ai_verdicts_count` (additive) counts items currently carrying a used
  `ai_decision`, so `ollama_used=false` + `ai_verdicts_count>0` reads as
  "serving cached AI verdicts", not "AI off". Settled outcomes keep monotonic
  AI provenance: at settlement, a prediction registered without a verdict is
  backfilled from the durable per-event registry for the same earnings event
  (`predicted_ai_provenance=ai_cache_backfill`) instead of settling null.
- `components.options_implied_move` is no longer populated from the
  recent-range proxy (the name claims options-implied volatility the system
  does not have); the honest proxy remains `components.range_proxy_move` /
  `range_proxy_pct`.
- `confidence_score` is always the 0-100 average of the two served
  `confidence` interval scores, including GapRisk-unavailable rows (thin data
  already widens the intervals); it is no longer forced to 0, which had
  contradicted the served interval object on the same row.

The contract is pinned by `tests/contract/schemas/pre_earnings.schema.json`
(validated by `tests/contract/test_pre_earnings_schema_contract.py`).

All outputs are explainable and traceable to cached inputs.

### Pulse persistence and event rollover

Every material Pulse generation is written atomically to
`pre_earnings_pulse_history.json`. Identical refresh copies are compacted by a
content fingerprint and increment `occurrences`/`last_generated_at`; unique
history is never pruned automatically. Active picks are keyed by ticker plus
earnings event date:

- a newly qualifying ticker is appended to the current long/short union;
- an omitted top-three ticker remains active while its same earnings event is
  still upcoming;
- the same ticker/event is updated in place without duplicates, and an older
  imported snapshot cannot overwrite a newer reason or direction;
- a reported, replaced, or expired undated event leaves the active card and is
  appended to `archive`; its historical snapshots remain intact;
- after restart, the last sidecar snapshot satisfies the normal Pulse TTL, so
  a page refresh or process restart does not immediately repeat the LLM call.
- an item with `flags.already_reported=true` is excluded from prompt
  descriptors and active picks. A same-event recommendation already present in
  the registry is moved to `archive` with
  `archive_reason=already_reported_news`; its original snapshot/history is
  retained for audit.
- imported or recovered snapshots may retain an ineligible reported pick in
  history, but cannot reactivate it. Eligibility is evaluated against the
  current event rows, not trusted from historical model output.
- initialization reconciles the loaded main snapshot and Pulse sidecar before
  serving cache-only state. If news-confirmed reported picks are removed, the
  reconciled active Pulse is persisted back to both
  `pre_earnings.json` and `pre_earnings_pulse_history.json` without an LLM or
provider call, so the first GET after restart cannot resurrect stale picks.
- a pick absent from the current valid shortlist remains in durable history but
  is not returned through `current_top_long` / `current_top_short`.
- the exclusion is event-scoped, not a permanent symbol blacklist. A later
  quarterly event with a new event key and no `already_reported` flag can
  qualify and re-enter the active union normally.

`PreEarningsAgent.import_pulse_snapshot(...)` can recover an exported historical
snapshot without any provider call. The import records its source and event
metadata and safely merges only picks whose current event is still active.

### Restart scheduling

Startup is cache-first. If `pre_earnings.json` contains a non-empty snapshot
whose `status.last_success_time` is still inside `refresh_seconds`, the worker
waits only the remaining interval instead of immediately starting a provider
or LLM cycle. Empty, invalid, or stale snapshots remain due immediately.
Explicit `Refresh` / `Refresh + Enrich` requests bypass this wait. A failed
automatic cycle observes the normal retry interval, preventing a tight
provider-call loop after restart.

## Refresh + Enrich

`Refresh + Enrich` is intentionally different from a page reload:

- A symbol refresh resolves that ticker's canonical earnings event first, then
  warms its news, EPS history, quote, daily bars, analyst, and trend inputs.
- Completed EPS estimate/actual pairs come from the economic-calendar cache
  when sufficiently populated, with a verified Yahoo Finance fallback. Empty
  or failed Yahoo responses are not negative-cached, so a later explicit
  refresh can retry instead of leaving `N/A` frozen for hours.
- Once enrichment finishes, only that symbol is rebuilt and atomically
  persisted. It does not wait for a full 200-symbol scan. A concurrent global
  cycle overlays the newer targeted row instead of overwriting it.
- `coverage_score` measures fields actually obtained; refresh attempts do not
  fabricate 100%. The UI lists missing contributors such as `news`, `history`,
  or `range`. A symbol reaches 100 only when every weighted input is present.
- Global refresh enriches a small rotating low-coverage batch. Explicit symbol
  requests are queued and prioritized, deduplicated, and bounded by
  `coverage_warmup_max_symbols` (default 5).
- Native yfinance operations and wrapped HTTP calls share the agent's external
  call budget. Exhaustion is logged once per run context; cached last-good
  values remain available.
- Verified enrichment is carried forward only for the same symbol and earnings
  event window. Beat history, historical move samples, recent reactions,
  analyst consensus, range proxy, liquidity/market-cap context, earnings
  trend, and news context each retain provenance in
  `enrichment_value_sources` plus a per-field verification timestamp.
- A failed or poorer provider response never replaces a better verified
  same-event value with `N/A`. Per-field `enrichment_refresh_after_utc`
  cooldowns survive persistence and restart, so scheduled cycles do not
  repeatedly spend the external-call budget on fields that just failed.
  Explicit per-symbol enrichment may retry; the next quarterly event starts
  clean and cannot inherit these values.

## Desktop UI contract

The current premium pass targets desktop only. The filter command center uses
three balanced desktop groups (Calendar & Universe, Signal Quality, View &
Search), while Pulse picks are stacked one ticker per row and its saved
history is shown newest-first in a collapsible premium panel. The card uses the
site's cream/glass visual tokens, aligned labels, bounded content, and
click-through ticker actions. Responsive/mobile refinement is intentionally
deferred; no mobile-layout completion is claimed by this release.

## Limitations

1. Not a perfect predictor
- The module is deterministic and explainable, but cannot guarantee exact
  post-earnings direction or magnitude.

2. Data availability constraints
- Missing components (options, sometimes analyst/fundamentals) stay `None`
  and are excluded from the weighted average.
- Signals degrade gracefully but may be less sharp on sparse symbols.

3. Proxy inputs
- Some advanced factors use local cache proxies when dedicated services are
  absent.

4. AI participation is cloud-only
- The LLM verdict/score blend runs only when an Ollama cloud key is
  configured. Without it, the structured data-driven call stands and no fake
  AI signal is emitted.

## Practical Usage

Use combined filters:
- First: `GapRisk >= 60` for event significance.
- Then split by `direction_label`:
  - `SHORT`: shortlist short-bias names
  - `LONG`: shortlist long-bias names
- Cross-check the `ai_decision` chip/card for the LLM's explicit call.
- Validate with `why`, `contributors`, and cached `top_news`.

## Resolved Improvements (previously listed as TODO)

1. ~~Replace 4H bars with true daily open/close cache for tighter event-window
   precision.~~ Done — `_load_symbol_daily_bars` + daily reference prices.

## Future Improvements

1. Add time-decay weighting on outcomes so recent regimes matter more.
2. Add separate calibration for AMC vs BMO and by liquidity regime.
3. Add rolling drift alerts when calibration degrades beyond threshold.
4. Validate the automatic quarterly retuning and confidence-interval width
   with regression tests.
