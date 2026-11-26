# Phase 10 implementation plan

1. Preserve and inspect the V7 News ingestion/normalization/deduplication path.
2. Add a pinned modern FinBERT adapter and an explicitly labelled fallback.
3. Add a standalone deterministic V9 news-pipeline contract for replay/tests.
4. Add edgartools adapter, canonical SEC metadata, cache-first PIT service, and API.
5. Replace the Phase 10 SEC AI-tool stub with the real service.
6. Quarantine instruction-shaped external News/SEC text before AI composition.
7. Add unit, contract, golden, and retained-News regression tests.
8. Verify dependencies on Python 3.14 and execute a real FinBERT inference.
9. Run curated/full regression, update V9 controls, and checkpoint.
