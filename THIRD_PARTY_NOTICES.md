# THIRD_PARTY_NOTICES

Open-source licenses and notices for production dependencies used by Market Signals V9
(spec §24). V9 is an evolution of the V7 codebase; V7's existing production stack is
carried forward and listed below, alongside the open-source repos Phase 2 decided to
INTEGRATE or ADAPTER (see `docs/V9_OPEN_SOURCE_MANIFEST.md`). REFERENCE_ONLY and REJECT
repos are NOT production dependencies and are not listed here (see footer).

Date generated: 2026-08-11 (Phase 2).

## 1. V9 production stack (carried from V7 `requirements.txt`)

These are the runtime production dependencies already pinned in V7 and inherited by V9.
Licenses are per package metadata / LICENSE files.

| Package | Version (pin) | License | URL |
|---|---|---|---|
| fastapi | 0.135.1 | MIT | https://github.com/fastapi/fastapi |
| uvicorn[standard] | 0.41.0 | BSD-3-Clause | https://github.com/encode/uvicorn |
| pandas | 3.0.1 | BSD-3-Clause | https://github.com/pandas-dev/pandas |
| numpy | 2.4.2 | BSD-3-Clause | https://github.com/numpy/numpy |
| yfinance | 1.2.1 | Apache-2.0 | https://github.com/ranaroussi/yfinance |
| curl-cffi | 0.15.0 | MIT | https://github.com/lexiforest/curl-cffi |
| lxml | 6.1.1 | BSD-3-Clause | https://github.com/lxml/lxml |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| feedparser | 6.0.12 | BSD-2-Clause | https://github.com/kurtmckee/feedparser |
| ta | 0.11.0 | MIT | https://github.com/bukosabino/ta |
| pytz | 2026.1.post1 | MIT | https://github.com/stub42/pytz |
| pydantic | 2.12.5 | MIT | https://github.com/pydantic/pydantic |
| python-dotenv | 1.2.2 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| aiofiles | 25.1.0 | Apache-2.0 | https://github.com/Tinche/aiofiles |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |

Development/test-only dependencies (pytest, etc.) are not production dependencies and
are not listed here.

## 2. Open-source repos Phase 2 decided INTEGRATE or ADAPTER

These repos are evaluated in `docs/V9_OPEN_SOURCE_MANIFEST.md` per spec §10. They are
NOT yet imported in Phase 2 (§118 — Phase 2 records decisions + pins only); the consuming
phase `pip install`s the pinned version against the manifest row. Listed here so the
attribution/notice obligation is recorded up front (§24).

### Microsoft Qlib
- URL: https://github.com/microsoft/qlib
- Release inspected: 0.20.1 (2026-04-21)
- License: BSD-3-Clause
- Integration type: ADAPTER (Phase 6 / Phase 14)
- Copyright (c) Microsoft Corporation. All rights reserved.
- Notice: Redistribution and use in source and binary forms, with or without
  modification, are permitted under the terms of the BSD-3-Clause license.

### skfolio
- URL: https://github.com/skfolio/skfolio
- Release inspected: 0.25.0 (2026-05-31)
- License: BSD-3-Clause
- Integration type: INTEGRATE (Phase 9)
- Copyright (c) skfolio contributors.
- Notice: BSD-3-Clause — redistribution and use permitted with the license terms.

### edgartools
- URL: https://github.com/dgunning/edgartools
- Release integrated: 5.48.0 (2026-08-13)
- License: MIT
- Integration type: INTEGRATE (Phase 10)
- Copyright (c) dgunning.
- Notice: MIT — permission is granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files, to deal in the Software without
  restriction. SEC identity is supplied via environment variable, never committed (§106).

### FinBERT
- URL: https://github.com/ProsusAI/finBERT
- Adapter runtime: transformers 5.15.0 + torch 2.13.0 + Hugging Face model
  `ProsusAI/finbert` revision `4556d13015211d73dccd3fdd39d39232506f3e43`
- License: Apache-2.0 upstream repository work; the licensed upstream repository
  explicitly links this published model
- Integration type: ADAPTER (Phase 10), via the maintained modern Transformers pipeline
  (do NOT use the obsolete `pytorch_pretrained_bert` from the ProsusAI repo — §16)
- Copyright (c) ProsusAI.
- Notice: Apache-2.0. Model source, immutable revision, and labelled fallback are
  retained in every V9 sentiment-quality record.

### Transformers
- URL: https://github.com/huggingface/transformers
- Version: 5.15.0
- License: Apache-2.0
- Purpose: maintained text-classification runtime for the FinBERT adapter.

### PyTorch
- URL: https://github.com/pytorch/pytorch
- Version: 2.13.0
- License expression: Apache-2.0 with bundled permissive component notices.
- Purpose: FinBERT tensor inference runtime. See the installed PyTorch distribution
  for the complete bundled third-party notice set.

### MLflow
- URL: https://github.com/mlflow/mlflow
- Release inspected: 3.15.1 (2026-08-03)
- License: Apache-2.0 (Databricks)
- Integration type: ADAPTER (Phase 14; MLflow AI Gateway evaluated in Phase 3)
- Copyright (c) Databricks, Inc.
- Notice: Apache-2.0. V9 keeps its own `AIProvider` interface (§17); MLflow is behind an
  adapter/service boundary; V9 domain code does not depend on MLflow internals.

### Optuna
- URL: https://github.com/optuna/optuna
- Release inspected: 4.9.0 (2026-06-01)
- License: MIT
- Integration type: INTEGRATE (Phase 6 / Phase 14)
- Copyright (c) Optuna Contributors.
- Notice: MIT. Optuna optimizes ONLY on allowed research/training/validation data, NEVER
  the final untouched holdout; multi-objective; explicit trial budgets (§18).

## 3. NOT listed (REFERENCE_ONLY / REJECT)

The following evaluated repos are NOT production dependencies of V9 and do not appear in
the attribution list above (they are recorded in `docs/V9_OPEN_SOURCE_MANIFEST.md`):

- Microsoft RD-Agent — REFERENCE_ONLY (research server, Docker/WSL, not a runtime dep)
- Earnings-Volatility-Calculator — REFERENCE_ONLY (methodology only, no code copied)
- RiskLabAI.py — REFERENCE_ONLY (license verified BSD-3-Clause, but prefer published
  methodology + independent V9 implementation; no code copied in Phase 2)
- quant-garage — REFERENCE_ONLY (AGPL-3.0; concepts only; no copy/vendor/hidden-dep)
- River — REFERENCE_ONLY (Phase 2; detection-only initially; may upgrade to ADAPTER in
  Phase 6)
- vectorbt — REJECT (Apache-2.0 + Commons Clause fair-code license; §20 license gate not
  granted; V9 stays functional via InternalBacktestEngine)
- CCXT — REJECT (V7 crypto execution sufficient, §23)

## License note

All licenses above are standard permissive OSI licenses (MIT, Apache-2.0, BSD-2-Clause,
BSD-3-Clause). No copyleft (GPL/AGPL) or fair-code (Commons Clause) license is a
production dependency of V9; vectorbt (Commons Clause) was REJECTED precisely on license
grounds (§20). This file is maintained alongside the manifest; when a consuming phase
merges one of the INTEGRATE/ADAPTER deps, the pin and notice here are the authority.
