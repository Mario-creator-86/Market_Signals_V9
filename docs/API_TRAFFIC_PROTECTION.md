# API Traffic Protection

Market Signals rejects abusive or capacity-exhausting API work before endpoint
logic, provider calls, or database mutations run.

## Controls

- Sliding per-client buckets are separate for reads, mutations, and login
  attempts.
- Bucket cardinality is bounded and maintenance is amortized; a source-address
  flood cannot grow memory without limit or force a full-table sweep on every
  request.
- Declared and streamed request bodies are bounded. Oversized bodies receive
  `413` even when no `Content-Length` is present.
- Process-wide and per-client in-flight request counters protect the event loop,
  thread pool, SQLite, and external provider budgets.
- Rejections include a normalized request ID, `Retry-After` where applicable,
  rate-budget headers, `Cache-Control: no-store`, and defensive browser headers.
- Login attempts use the strictest rate class.
- Production startup fails if traffic control is disabled or any limit is
  malformed or outside its safe range.

`/api/health` is intentionally exempt so the orchestrator can always evaluate
liveness. Readiness and diagnostics remain protected.

## Response semantics

| Status | Meaning |
|---|---|
| `400` | Invalid `Content-Length`. |
| `413` | Declared or streamed request body exceeded the configured limit. |
| `429` | Client rate, per-client concurrency, or bucket capacity was exceeded. |
| `503` | Process-wide in-flight capacity was exhausted. |

Clients should honor `Retry-After` and use exponential backoff with jitter.

## Monitoring

The authenticated OpenMetrics endpoint exports:

- `market_signals_api_inflight_requests`
- `market_signals_api_inflight_limit`
- `market_signals_api_rate_limit_buckets`
- `market_signals_api_request_body_limit_bytes`
- `market_signals_api_admission_rejections_total{reason=...}`

Diagnostics expose the same bounded aggregate state under `api_traffic`.
No client address or credential is exported.

## Scaling boundary

These controls protect one API process, matching the supported single-replica
Compose deployment. For multiple replicas, apply a shared rate and body-size
policy at the trusted ingress/API gateway as well. Keep the in-process controls
enabled as the final containment layer. Only trust forwarded client addresses
from the configured reverse proxy; direct public access to the application
listener is unsupported.
