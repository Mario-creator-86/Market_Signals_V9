# Phase 10 — News, FinBERT, and SEC design

## Goal

Complete sections 14, 16, 68, and 69 without replacing the working V7 News
agent. V9 owns a modular point-in-time evidence boundary; V7 provider fetchers,
UI, persistence, and deduplication remain in service.

## Architecture

```text
V7 news providers -> NewsTabAgent normalize/dedup/entity/category
                                   |
                                   +-> FinBERT adapter -> labelled lexicon fallback
                                   +-> point-in-time evidence ledger
                                   +-> quarantined AI tool envelope

EDGAR_IDENTITY -> EdgarToolsAdapter -> SECFilingService -> atomic TTL cache
                                                |             |
                                                |             +-> PIT filter
                                                +-> /api/sec/{ticker}
                                                +-> get_sec_context AI tool
```

## Decisions

- Transformers 5.15.0 and torch 2.13.0 are pinned. The adapter loads
  `ProsusAI/finbert` at immutable revision
  `4556d13015211d73dccd3fdd39d39232506f3e43`; it never uses the obsolete
  `pytorch_pretrained_bert` implementation.
- FinBERT is lazy and local-cache-first. If the runtime/model is unavailable,
  the proven V7 lexicon may continue only with `fallback_used=true` and a
  distinct `sentiment_lexicon` source.
- edgartools 5.48.0 is the SEC adapter. A real `EDGAR_IDENTITY` is mandatory,
  metadata is cached atomically, unchanged filings are not re-requested inside
  TTL, and historical reads exclude filings not available at the requested T.
- News and filing strings are untrusted data. Instruction-shaped strings are
  replaced by a SHA-256 quarantine marker before any AI provider sees them.
- The SEC integration fetches bounded filing metadata only. It does not
  implicitly download or prompt over entire filing bodies.

## Failure behavior

- Missing FinBERT runtime/model: explicit fallback or unavailable result.
- Missing SEC identity/provider/network: stale cache if available, otherwise a
  bounded `unavailable` response; trading continues without fabricated SEC data.
- Invalid ticker/form/non-SEC URL: rejected.
- Suspicious external instruction text: quarantined, never passed through.

## Phase boundary

The existing News UI remains the operational UI. A dedicated SEC/health panel
and operator settings belong to Phase 12. Autonomous Paper order composition of
this evidence belongs to Phase 11.
