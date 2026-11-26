# V9_DATA_POLICY

Living document. Structure from spec §27–§33. Implementation `NOT STARTED` at Phase 0; built in Phase 1.

## Canonical data layer (§27)
Entities: symbol, asset, exchange, currency, timezone, trading session, OHLCV, corporate actions, earnings, analyst estimates, fundamentals, options, news, SEC, signals. Prefer UTC internally; exchange-local time only when required.

## Market calendar (§28)
Understand exchange timezone, holidays, half days, pre-market, regular session, after-hours, BMO, AMC. Do not assume every weekday is a trading day.

## Point-in-time data (§29)
Preserve where appropriate: event_time, publication_time, provider_time, received_at, available_from, fetched_at, version. At historical timestamp T, the system may use only data available at T.

## Survivorship bias (§30)
Historical universe tests must not silently use only current surviving securities. Account for delisted stocks, historical index membership, ticker changes, mergers, bankruptcies where data permits. If unavailable, document the limitation; do not pretend it is solved.

## Corporate actions (§31)
Handle splits, dividends, ticker changes, mergers. Define adjusted/raw price conventions clearly.

## No synthetic historical facts (§32)
Do NOT reconstruct unavailable historical facts from future data. Examples: old option IV from today's chain; historical analyst consensus from today's revised value; old sentiment from later articles. Missing historical info remains UNAVAILABLE unless a valid point-in-time source exists.

## Data quality (§33)
Every important analysis exposes: source, timestamp, freshness, coverage, stale, conflicts, quality_flags. Separate SIGNAL/ANALYSIS QUALITY from DATA QUALITY.

## Phase 10 external-text evidence

- News observation time remains distinct from publication time. Syndicated copies are grouped and marked, not counted as independent evidence.
- SEC filing event time is the filing date; `available_from` is when V9 fetched/observed the metadata. Historical `as_of` reads exclude later observations even if the filing date itself is earlier.
- Missing title/source/URL/timestamp is rejected rather than fabricated. SEC URLs must resolve to HTTPS on `sec.gov` or a subdomain.
- News and SEC strings are untrusted external data. Instruction-shaped content is quarantined before AI use; the audit digest is retained without exposing the command text to the model.

## Phase 11 execution evidence

- `execution.db` is the durable Paper-order source of truth for intent identity, lifecycle transitions, broker result/error, and reconciliation status. Runtime DB/WAL/SHM files are never committed.
- Repeated scheduler delivery retains one order per unique idempotency key. Events are append-only; a state transition never overwrites the historical event sequence.
- API order reads are bounded and expose summaries, not arbitrary metadata. Reconciliation repairs only outcomes supported by authoritative Capital position state; uncertainty is retained as `EXECUTION_STATE_CONFLICT`, never fabricated as a fill.
