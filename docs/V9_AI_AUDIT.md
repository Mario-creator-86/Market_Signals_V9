# Market Scanner V9 — AI Audit

## Status

PASS for the configured Ollama path, decision/tool/safety contracts and deterministic fallbacks. Commercial provider live calls without owner credentials are `BLOCKED_EXTERNAL`.

## Provider matrix

| Provider | Configuration/schema | Discovery/completion | Structured/tool adapters | Failure/fallback | Result |
| --- | --- | --- | --- | --- | --- |
| Ollama | owner-selected endpoint/model, secret-safe status | live `glm-5.2:cloud` returned `V9_AI_OK` | real tool dispatch and decision pipeline pass | bounded unavailable/fallback pass | PASS |
| OpenAI | schema and secret handling verified | no API key | provider/tool parser tests pass | fail-closed/fallback pass | BLOCKED_EXTERNAL live |
| Anthropic | schema and secret handling verified | no API key | system/tool-use parser tests pass | fail-closed/fallback pass | BLOCKED_EXTERNAL live |
| Gemini | schema and secret handling verified | no API key | function-call parser tests pass | fail-closed/fallback pass | BLOCKED_EXTERNAL live |
| OpenAI-compatible | host/auth isolation verified | credential-dependent | shared structured contract passes | host-specific cooldown/fallback pass | BLOCKED_EXTERNAL live |

No secret value is exposed by configuration/status APIs or audit documents.

## Prompts, decisions and critic

- Role prompts cover Market Analyst, Pre-Earnings Analyst, News Analyst, Quant Analyst, Portfolio Manager, Risk Critic and Research Scientist.
- Prompt inputs are built from normalized bounded evidence, not raw page instructions.
- Provider, model, prompt version/hash, tool trace, structured decision, critic verdict and correlation IDs are persisted in decision evidence.
- The decision schema is strict and freezes the allowed actions. Invalid JSON, unknown fields/actions and missing provider output become deterministic `NO_TRADE`/unavailable results.
- The critic is a distinct post-decision stage. A critic veto forces no trade before risk/capital/execution.
- AI may propose an order; it cannot execute directly. The actual execution path still requires risk PASS, non-zero bounded capital, PAPER mode, idempotency and an inactive kill switch.

## Tool audit

The registry contains exactly 28 allow-listed tools across market data, earnings, history, news/sentiment, options, SEC, signals, strategy, backtest, portfolio/risk and order proposal. Representative dispatch was executed; all tool schemas validate arguments and convert external errors into bounded results. Unknown tool names are rejected. Registry tests also prove tools cannot spawn subprocesses or open arbitrary files.

## Injection and safety evidence

| Control | Actual result |
| --- | --- |
| News/SEC prompt injection | normalized as inert evidence; cannot change policy/tool authority |
| Unknown tool | rejected before dispatch |
| Malformed structured decision | fail-closed to no-trade |
| Critic veto | forces no-trade |
| Risk rejection | forces zero execution |
| Duplicate decision | one PAPER order only |
| Kill switch | blocks new risk while monitoring remains active |
| Execution mode | PAPER only; no real-money order path exercised |

Evidence includes `tests/golden/test_phase10_golden.py`, `test_ai_tool_registry_golden.py`, `test_ai_decision_pipeline_golden.py`, `test_ai_fallback_golden.py`, `test_phase4c_golden.py`, and the complete 265-test AI-focused run.

## ML/sentiment evidence

FinBERT loaded from the pinned local model revision and performed real inference (positive sample confidence `0.9275`). Model load can create a one-time startup latency spike; subsequent dashboard calls were 1.97–48ms and the observed global SLO remained compliant.

## External limitations

- `BLOCKED_EXTERNAL`: OpenAI, Anthropic, Gemini and credentialed compatible endpoints were not live-called without owner keys.
- `BLOCKED_EXTERNAL`: live SEC/Edgar queries require a compliant `EDGAR_IDENTITY`.
- These limitations do not bypass local provider-contract, fallback, safety or PAPER acceptance tests and are not reported as live passes.
