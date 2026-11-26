# Market Scanner V9 — Open-Source Reality Audit

Classification is based on installed metadata, source trace and executed workflow evidence—not names in comments or manifests.

| Project/methodology | Installed/runtime evidence | V9 usage and boundary | Classification |
| --- | --- | --- | --- |
| Microsoft Qlib | package absent; `core/ml/qlib_adapter.py` exists | optional adapter, no executed Qlib workflow | SCAFFOLDED / NOT_CONNECTED |
| Microsoft RD-Agent | package absent | design/reference influence only | REFERENCE_ONLY |
| skfolio | package absent | portfolio methodology/reference, not called at runtime | NOT_CONNECTED |
| EdgarTools | `edgartools 5.48.0`; adapter/unit tests pass | normalized SEC evidence path; live SEC requires identity | INTEGRATED; live `BLOCKED_EXTERNAL` |
| Earnings Volatility Calculator methodology | independently implemented formulas/tests | deterministic implied/realized move evidence in Pre-Earnings | METHODOLOGY_INTEGRATED |
| FinBERT | `transformers 5.15.0`, Torch and pinned model available | real local inference executed; positive confidence `0.9275` | INTEGRATED |
| MLflow | package/config absent | optional experiment tracking boundary only | NOT_CONNECTED |
| Optuna | `optuna 4.9.0` | real 5-trial persisted study `v9_audit_20260820`; promotion remains gated | INTEGRATED |
| RiskLabAI | package absent | research/reference concepts only | REFERENCE_ONLY |
| vectorbt | package absent; policy decision documented | not required and not imported; license/dependency surface avoided | REJECTED / NOT_CONNECTED |
| Quant Garage-derived concepts | no copied runtime package | provenance/reference only; V9 code independently tested | REFERENCE_ONLY |
| River | package absent | online-learning reference/optional future adapter | REFERENCE_ONLY |

## Dependency and license evidence

- `python tools/supply_chain_check.py` passed: 122 installed dependencies, zero known vulnerabilities.
- A CycloneDX runtime SBOM was generated at `output/supply-chain/market-signals-runtime.cdx.json`.
- `THIRD_PARTY_NOTICES.md` and supply-chain policy tests pass.
- No source-level evidence of copied package implementations, embedded credentials or an undeclared runtime dependency was found.
- Optional packages are fail-closed: absence cannot silently impersonate a real integration.

## External limitations

- `BLOCKED_EXTERNAL`: the live EdgarTools/SEC request cannot be compliant without owner-provided `EDGAR_IDENTITY`. Local parsing, schemas, normalization and failure handling pass.
- MLflow, Qlib, RD-Agent, skfolio, RiskLabAI, vectorbt and River are not claimed as live integrations.
