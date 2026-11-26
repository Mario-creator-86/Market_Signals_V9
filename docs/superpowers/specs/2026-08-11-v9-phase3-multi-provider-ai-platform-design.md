# Phase 3 — Multi-Provider AI Platform (Design)

**Date:** 2026-08-11
**Phase:** 3 (Multi-Provider AI Platform). Phase 2 (Open-Source Integration Foundation) = PASS as of `07f638f` (1651 tests green). Phase 1 (Data & Architecture Foundation) = PASS (`f1d65d4`).
**Spec basis:** `prompt_to_follow.md` §34 (AIProvider + 8 normalized capabilities), §35 (AI providers — Ollama Local/Cloud, OpenAI, Anthropic, Gemini + adapters for xAI/DeepSeek/Mistral/OpenRouter; do NOT hardcode model names; verify current official APIs at implementation time), §36 (Model Registry), §37 (AI Model Assignment — 10 roles, one-model-for-all allowed), §38 (AI Model Router — triage/analysis/critic tiers), §17 (MLflow — V9 keeps own `AIProvider`; MLflow AI Gateway may be evaluated as part of the multi-provider layer), §7/§34 ("Audit existing Ollama/Ollama Cloud integration first. Preserve what works. Refactor toward AIProvider"), §106 (secrets), §107 (GOLD-013 AI provider fallback, GOLD-014 all AI unavailable → no new autonomous trade), §108 (E2E AI configuration journey), §4 (no forbidden language), §5 (evolve, no rewrite), §115 (no test weakening), §117 (V7 read-only), §118 (deps), §25 (pin deps).

## 1. Audit findings (the starting point — "audit first, preserve what works")

A read-only audit of V9's current AI surface (see ledger) established:

- **Two modules own all AI.** `core/ollama_client.py` (`OllamaClient` + module singleton `ollama`) and `core/ollama_decision.py` (prompt builders + parsers). No provider abstraction exists (no ABC/Protocol/factory/registry).
- **One generation path.** `ollama.generate(prompt, model=None, system="", ui_call=False, timeout_override=None) -> dict` POSTs `{base}/api/chat` with `{model, messages, stream: False}`, normalizes chat `message.content` into `data["response"]`, attaches `data["_meta"]={model, generated_at, ui_call, timeout_sec}`, and on failure returns `{"error": "..."}`. No streaming, no `tools:`, no `format:` schema — JSON is requested **in-prompt** ("Output strict JSON only.") and parsed post-hoc by brace-matching `_extract_json_object`.
- **13 call sites** depend on this contract (11 direct `ollama.*` callers — watchlist_quick_ai, ticker_analyzer, actions_service, api/main status/set-model/news-analyze, premarket_scanner_agent, pre_earnings_agent, news_tab_agent; + 2 `ollama_decision` callers — agent_manager, capital_manager). All read `result["response"]`.
- **Health + models.** `check_availability()` GETs `/api/tags` (TTL-cached 30s/5s, thread-collapsed, registers `ollama_availability` in the health dashboard); `get_models()` GETs `/api/tags` (TTL 60s/10s). These already satisfy the §34 `health_check()`/`list_models()` capability shape under different names.
- **Local-vs-cloud routing** is automatic: if `OLLAMA_API_KEY` present + `use_cloud_when_api_key_present`, base URL flips to `https://ollama.com` (Bearer auth) and the active model becomes `cloud_model` (`gpt-oss:120b`). `is_cloud_host()` is the discriminator. `review["provider"]` is labeled `ollama_local`/`ollama_cloud` — the seed of provider identity.
- **Transport seam.** `core/http_utils.py` (`http_get`/`http_post`, `requests`-based) provides per-provider circuit breaker, rate limit, concurrency semaphore, and the ollama-only external-call-budget exemption (`provider="ollama"`). This is the governed transport new providers reuse.
- **Config.** `config["ollama"]` block (model/cloud_model/base_url/cloud_base_url/api_key_env/timeout/decision_*/exit_*) in `config.json` + `DEFAULT_CONFIG["ollama"]` in `core/config.py`. Env: `OLLAMA_API_KEY`, `OLLAMA_BASE_URL`, TTL envs, per-feature overrides. No OpenAI/Anthropic/Gemini env vars or clients exist.
- **Tests pinning the contract** (must stay green): `test_ollama_chat_endpoint.py` (`/api/chat` not `/api/generate`; `response` key; `_meta.model`), `test_ollama_cloud_host.py` (cloud host `https://ollama.com`; Bearer headers; TTL caching), `test_ollama_set_model_cloud.py`, `test_ollama_budget_exempt.py`, `test_premarket_ollama_chat_endpoint.py`, `test_ollama_decision.py`, `test_pre_earnings_pulse.py`, plus `tests/conftest.py` autouse `mock_external_deps` patching `OllamaClient.check_availability → False`.

**Implication:** the refactor is evolutionary. The existing `OllamaClient` becomes the `OllamaProvider`'s implementation; the `ollama` singleton stays as a backward-compat shim so all 13 callers keep working unchanged; new code goes through the `AIProvider` interface.

## 2. Verified provider REST shapes (§35 "verify current official APIs at implementation")

Inspected 2026-08-11 (recorded per §25). V9 talks to these over `core/http_utils` (requests, already pinned) — **zero new production dependencies**:

- **OpenAI** — `POST https://api.openai.com/v1/chat/completions`; `Authorization: Bearer <key>`; body `{model, messages, stream, response_format:{type:"json_schema",json_schema:{...}} | {type:"json_object"}, tools:[{type:"function",function:{name,description,parameters}}], tool_choice, temperature}`; response `choices[0].message.{content,tool_calls:[{id,function:{name,arguments(JSON string)}}]}`, `usage:{prompt_tokens,completion_tokens,total_tokens}`; streaming SSE chunks `object:"chat.completion.chunk"` with `choices[0].delta.content`. (Source: openai-openapi spec via Context7.)
- **Anthropic** — `POST https://api.anthropic.com/v1/messages`; headers `x-api-key: <key>`, `anthropic-version: 2023-06-01`, `content-type: application/json` (NOT `Authorization`); body `{model, messages, system, max_tokens (required), stream, tools:[{name,description,input_schema}], tool_choice, temperature}`; response `content[]` blocks (`text` / `tool_use:{id,name,input}`), `stop_reason`, `usage:{input_tokens,output_tokens}`; streaming SSE events `message_start`/`content_block_delta`/`message_delta`/`message_stop`. (Source: platform.claude.com docs.)
- **Gemini** — `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=<key>` (or `x-goog-api-key` header); body `{contents:[{role,parts:[{text}]}], systemInstruction, generationConfig:{responseMimeType:"application/json",responseSchema,...,temperature,maxOutputTokens}, tools:[{functionDeclarations:[{name,description,parameters}]}], toolConfig}`; response `candidates[0].content.parts[0].text`, `usageMetadata:{promptTokenCount,candidatesTokenCount,totalTokenCount}`; streaming via `:streamGenerateContent?alt=sse`. (Source: ai.google.dev docs.)
- **OpenAI-compatible** (xAI `https://api.x.ai/v1`, DeepSeek `https://api.deepseek.com/v1`, OpenRouter `https://openrouter.ai/api/v1`, Mistral `https://api.mistral.ai/v1`) — all expose OpenAI's `/v1/chat/completions` shape, so a single `OpenAICompatibleProvider` configured with `base_url` + `api_key_env` covers all four (§35 "where appropriate").

## 3. Architecture

```text
                          ┌─────────────────────────────────────────────┐
   callers (13 existing   │  ollama singleton (shim) → OllamaProvider    │  ← preserved
   + new role-based calls)│  agent_manager / capital_manager decision    │
                          │  layer (ollama_decision, unchanged)          │
                          └───────────────┬─────────────────────────────┘
                                          ▼
                          ┌─────────────────────────────────────────────┐
                          │  ModelRouter (§38)  →  tier (triage/         │
                          │   analysis/critic) → role → ModelAssignment │
                          │   (§37, 10 roles) → model_id                 │
                          │   generate_with_fallback (GOLD-013)         │
                          └───────────────┬─────────────────────────────┘
                                          ▼
                          ┌─────────────────────────────────────────────┐
                          │  AIProvider ABC (§34): list_models,         │
                          │   health_check, capabilities, generate,      │
                          │   generate_structured, tool_call, stream,    │
                          │   usage                                      │
                          └───────────────┬─────────────────────────────┘
                                          ▼
   OllamaProvider   OpenAIProvider   OpenAICompatibleProvider   AnthropicProvider   GeminiProvider
   (local+cloud,    (api.openai.com)  (xAI/DeepSeek/             (api.anthropic.com) (generativelanguage
    evolve Ollama-                     Mistral/OpenRouter —                            .googleapis.com)
    Client; preserve                   OpenAI chat shape)
    /api/chat)
                                          ▼
                          ┌─────────────────────────────────────────────┐
                          │  core/http_utils (requests, PINNED) —        │
                          │  circuit breaker / rate limit / concurrency  │
                          │  / budget / observability, per provider name │
                          │  (new names added: openai/anthropic/gemini/  │
                          │   xai/deepseek/mistral/openrouter)           │
                          └─────────────────────────────────────────────┘
                                          ▼
                          ┌─────────────────────────────────────────────┐
                          │  ModelRegistry (§36): provider, model_id,    │
                          │   display_name, capabilities, tool_support,  │
                          │   structured_output_support, context_metadata│
                          │   health, enabled, last_checked — discover  │
                          │   via list_models(), manual config otherwise │
                          └─────────────────────────────────────────────┘
```

`api/ai_config_service.py` + `api/main.py` routes expose the §108 E2E journey (configure → test → discover → assign → save → validated request). API keys never leave the backend (only `has_key: bool` to the frontend, §106).

## 4. Key autonomous decisions

1. **Zero new production dependencies (§118/§25).** All providers talk over `core/http_utils` (`requests`, already a pinned V7 dep). SSE for `stream()` is parsed from `requests` `iter_lines()` (a few lines of line-based parsing) — `httpx-sse` is NOT added. The installed `openai`/`anthropic`/`google-genai` SDKs are present in the shared venv but are **not** pinned in `requirements.txt`; using them would mean adding unpinned production deps (violates §25) or pinning 3 SDKs + transitive trees. httpx-direct over the governed `http_utils` seam is lower-risk (§106 — no SDK telemetry surface), fully testable without keys/network (injectable transport), and consistent with the existing Ollama code. The Phase 2 manifest's MLflow/Optuna/etc. rows are unaffected; the AI-provider SDKs are not §11–§23 repos and need no manifest row (they are not adopted — V9 owns the provider implementations).
2. **Evolutionary, not a rewrite (§5/§7/§34).** `OllamaProvider` evolves `OllamaClient`; the `ollama` singleton becomes a shim delegating to it; all 13 call sites and the 8 pinned Ollama tests stay green unchanged. `ollama_decision` (prompt builders/parsers/veto/exit-guard) is already provider-agnostic and is the first consumer of the new `AIProvider.generate_structured` seam — but only behind the existing `ollama.generate` shim, so no caller changes in 3A.
3. **No hardcoded model names (§35).** Code never references a model id. `list_models()` discovers; config supplies default model ids (Ollama defaults preserved from V7 — `gemma3:12b`/`gpt-oss:120b`; new-provider defaults are `""` → discover-then-select per the §108 flow). The §108 journey is "discover models → select role models → save" — selection is explicit.
4. **Injectable transport for testability.** Each provider takes an `http` callable seam (default: `core.http_utils.http_post`/`http_get`) so unit tests inject a fake transport asserting the exact request shape and returning canned responses — no live API calls, no keys. This is how the verified REST shapes in §2 become executable tests.
5. **§106 secrets.** API keys are read from env vars named by `api_key_env` (or a config `api_key` value, gitignored like V7's `config.json`). Keys are never put in prompts, never returned to the frontend (only `has_key`), never logged (the existing `_cache_identity` deliberately omits the key value — preserved). The `provider="..."` string in http_utils carries no secret.
6. **MLflow AI Gateway (§17 line 728) — evaluated, not adopted in Phase 3.** The MLflow AI Gateway exposes an OpenAI-compatible endpoint for many models behind one gateway. V9's `OpenAICompatibleProvider` can point at an MLflow AI Gateway `base_url` with no MLflow dependency — so the multi-provider layer already *accommodates* it. Phase 3 records this evaluation (ADAPTER-noted, deferred); the full MLflow experiment-tracking integration is Phase 14 per the manifest. This honors §17 ("V9 keeps own AIProvider") and "may be evaluated as part of the multi-provider layer."
7. **Phase boundary (§39 tools, §41 decisions → Phase 4).** Phase 3 delivers the *platform*: AIProvider + providers + registry + assignment + router + fallback + §108 config routes. `tool_call()` is implemented as the provider round-trip (send `tools`, parse `tool_calls`) — but the §39 tool *registry* (get_market_snapshot, etc.) and the §41 structured decision schema and the independent-critic *agent loop* are Phase 4. The §38 router's "independent model → critic" is delivered as a tier the router can select; the critic *workflow* is Phase 4.

## 5. Config schema (additive — `config["ai"]`, alongside preserved `config["ollama"]`)

```jsonc
"ai": {
  "providers": {
    "ollama":       { "enabled": true,  "type": "ollama" },                       // reads existing config["ollama"]
    "openai":       { "enabled": false, "type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "default_model": "", "timeout": 60 },
    "anthropic":    { "enabled": false, "type": "anthropic", "base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY", "default_model": "", "timeout": 60 },
    "gemini":       { "enabled": false, "type": "gemini", "base_url": "https://generativelanguage.googleapis.com", "api_key_env": "GEMINI_API_KEY", "default_model": "", "timeout": 60 },
    "xai":          { "enabled": false, "type": "openai_compatible", "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY", "default_model": "" },
    "deepseek":     { "enabled": false, "type": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY", "default_model": "" },
    "mistral":      { "enabled": false, "type": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY", "default_model": "" },
    "openrouter":   { "enabled": false, "type": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "default_model": "" }
  },
  "registry": [],          // §36 model entries (provider/model_id/display_name/capabilities/tool_support/structured_output_support/context_metadata/health/enabled/last_checked), filled by discovery
  "assignment": {          // §37 — 10 roles; "all" alias sets every role
    "all": null,           // if set, applies to every role unless overridden
    "market_analyst": null, "pre_earnings_analyst": null, "news_analyst": null,
    "quant_analyst": null, "portfolio_manager": null, "risk_critic": null,
    "research_scientist": null, "coding_agent": null, "code_reviewer": null, "security_reviewer": null
  },
  "router": {              // §38 — tiers
    "triage":   { "model": null },   // fast/cheap → initial triage
    "analysis": { "model": null },   // strong reasoning → deeper analysis
    "critic":   { "model": null }    // independent model → critic
  },
  "fallback": {            // GOLD-013
    "enabled": true,
    "chain": []            // provider names tried in order after the assigned one fails
  },
  "all_unavailable_no_trade": true    // GOLD-014
}
```

`config.example.json` mirrors this; `DEFAULT_CONFIG["ai"]` in `core/config.py` provides defaults. `config.json` (gitignored) is user-edited. Existing `config["ollama"]` is untouched — `OllamaProvider` reads it for backward compatibility.

## 6. Decomposition into sub-plans (mirrors Phase 1's 1A/1B/1C)

- **3A — AIProvider interface + OllamaProvider (evolve) + config schema + transport seam.** New `core/ai/` package: `provider.py` (`AIProvider` ABC, 8 capabilities, `Usage`/`Capabilities`/`AIResponse` value types, injectable `http` seam), `ollama_provider.py` (`OllamaProvider` evolving `OllamaClient`; preserves `generate()` → `{response,_meta}`/`{error}`; implements `list_models`/`health_check`/`capabilities`/`generate`/`generate_structured`/`stream`/`usage`/`tool_call`), `__init__.py`. `core/ollama_client.py` `ollama` singleton becomes a shim delegating to `OllamaProvider` (all 13 callers + 8 tests unchanged). `core/http_utils.py` adds the new provider governor entries. `core/config.py` + `config.example.json` add `DEFAULT_CONFIG["ai"]`. Unit tests with the existing `mock_external_deps` fixture + fake transport. Baseline 1651 stays green.
- **3B — Cloud providers + Model Registry.** `core/ai/openai_provider.py`, `anthropic_provider.py`, `gemini_provider.py`, `openai_compatible_provider.py` (xAI/DeepSeek/Mistral/OpenRouter) — each over `http_utils` with an injectable transport, implementing the 8 capabilities per the verified §2 REST shapes (auth headers, request body, response parsing, SSE `stream()`, `tool_call()` round-trip, `usage()`). `core/ai/registry.py` `ModelRegistry` (§36 fields, discovery via `list_models()`, manual config, persistence). Fake-transport unit tests asserting exact request shapes + canned responses (no live calls, no keys).
- **3C — Assignment + Router + fallback + §108 API + golden tests.** `core/ai/assignment.py` (§37, 10 roles + `all`), `core/ai/router.py` (§38, triage/analysis/critic tiers, `select_model(role, tier_hint)`), `core/ai/fallback.py` (`generate_with_fallback` — GOLD-013 chain; all-unavailable → `UNAVAILABLE` sentinel → `agent_manager` autonomous guard treats as no-new-autonomous-trade — GOLD-014). `api/ai_config_service.py` + `api/main.py` routes for the §108 journey (providers/test/models/discover/assignment/request; keys never to frontend). MLflow AI Gateway evaluation recorded. Golden tests GOLD-013 (fallback), GOLD-014 (all-unavailable → no new autonomous trade). Gate → Phase 3 PASS.

## 7. Risks and mitigations

- **Breaking the 13 Ollama call sites / 8 pinned tests** — mitigated by the `ollama` singleton shim (3A keeps every caller unchanged; the shim delegates to `OllamaProvider`; the pinned tests assert on `ollama.generate`/`/api/chat`/`response` key, all preserved). The full regression is the gate every sub-plan.
- **REST drift / "verify current official APIs"** — mitigated by §2 (inspected 2026-08-11) + fake-transport tests that pin the exact request shape we send (so a future API change shows as a test diff, not a silent runtime bug). Providers are config-driven (`base_url`); endpoint paths are the verified current ones.
- **§106 key leakage** — mitigated: keys via `api_key_env`/gitignored `config.json`; `provider="name"` carries no secret; frontend gets only `has_key`; the existing key-omitting `_cache_identity` is preserved.
- **No live API in CI** — mitigated by the injectable transport + the existing autouse `mock_external_deps` fixture (extends to patch new providers' `health_check`).
- **Scope creep into Phase 4 (tools/decisions/critic workflow)** — mitigated by the explicit §4 decision: `tool_call()` is the provider round-trip only; the tool registry + §41 decision schema + critic loop are Phase 4.
- **Python 3.14.3** — no new deps, so no compat risk (requests already works on 3.14).

## 8. Out of scope (deferred, with phase owners)

- §39 tool registry + tool implementations (get_market_snapshot, run_backtest, …) → Phase 4.
- §41 structured decision schema + the critic agent loop → Phase 4.
- §37 model-assignment **UI** (the §108 frontend) → Phase 12 (Phase 3 ships the backend + API only).
- MLflow full experiment-tracking integration → Phase 14 (manifest ADAPTER).
- Optuna hyperparameter search → Phase 6/14 (manifest INTEGRATE).

## Self-review

- **Spec coverage:** §34 (AIProvider + 8 caps) → 3A ABC + 3B providers; §35 (providers, no hardcoded names, verify APIs) → §2 + 3A/3B + config defaults `""`; §36 (Model Registry) → 3B; §37 (Assignment, 10 roles, one-for-all) → 3C; §38 (Router triage/analysis/critic) → 3C; §17 (own AIProvider; MLflow AI Gateway evaluated) → §4 decision 6 + 3C record; §7/§34 (audit first, preserve) → §1 + the `ollama` shim; §106 (secrets) → §4 decision 5; §107 GOLD-013/014 → 3C; §108 (E2E journey) → 3C API; §4/§5/§115/§117/§118/§25 honored throughout. ✓
- **Placeholder scan:** no TBD/TODO in steps; provider defaults are `""` (intentional discover-then-select, not a placeholder). ✓
- **Ambiguity check:** "evolve not rewrite" = `OllamaProvider` wraps `OllamaClient` + `ollama` shim (concrete); "tool_call" = provider round-trip only (Phase 4 owns the registry); "MLflow AI Gateway evaluated" = accommodated via `OpenAICompatibleProvider`, not adopted (concrete §4 decision 6). ✓

No issues found. Design is complete.