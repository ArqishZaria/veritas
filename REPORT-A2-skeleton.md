# Assignment 2 report — skeleton (fill the bracketed parts from YOUR runs; write explanations in your own words)

## A1. REST constraints (draft entries matching this API)
| Constraint | Meaning (your words) | Example in Veritas |
|---|---|---|
| Client–server | UI and API separated, evolve independently | Browser `index.html` calls `/api/v1/*`; same API used by Python scripts |
| Stateless | Each request carries all info needed | `user_id` in every body/query; no server session — **but** exam session state is a server *resource*, addressed by id |
| Cacheable | Responses say if they may be cached | **Not demonstrated** (no Cache-Control/ETag) — [or add it to GET /quizzes] |
| Uniform interface | Resources by URI, standard methods, self-descriptive messages, hypermedia | `PUT /sessions/{id}/attempt`, `POST …/sessions` returns `Location`; no HATEOAS links (say so) |
| Layered system | Client can't tell if it talks to the end server | Prometheus/Grafana/browser only see HTTP; [no proxy yet — say so] |
| Code on demand (optional) | Server ships executable code | Server sends JS in `index.html` |

## A2. OpenAPI (2–3 sentences, yours) + how you used it
Facts to build from: OpenAPI is a language-neutral, machine-readable description of an HTTP API (paths, methods,
params, bodies, responses); used for docs, client/server codegen, validation and contract tests.
Use: `openapi/openapi.json` generated from FastAPI models → validated by `openapi-spec-validator` in `pytest`,
drift check test, and Swagger UI `/docs` *Try it out* on `PUT /sessions/{id}/attempt` ([paste screenshot]).

## A3. Before/after for the user task "Take a quiz" (submit step)
| | Old | Problem | Change | Reason |
|---|---|---|---|---|
| Endpoint | `POST /api/exam/submit` (body: quiz_id, user_id, answers, tab_switch_count) | Verb in path; a retry/double-click created a 2nd attempt and decremented the active gauge twice; cheating count trusted from client; 404 vs validation inconsistent; `{"detail":…}` errors | `PUT /api/v1/sessions/{session_id}/attempt`; server-issued session id = idempotency key; server counts tab switches; 201/200/409; unified error body | Safe retries; resource-oriented path; server is authority |
Other endpoints changed: [use the endpoint map in README-A2]. Left alone: `/health`, `/metrics` (excluded), [list any].

## A4. Idempotency
Operation: submit. Mechanism: unique `session_id` → one `attempts` row (`UNIQUE(session_id)`), decided inside a
`BEGIN IMMEDIATE` transaction. Same answers → stored result (200, `Idempotent-Replay`); different → 409.
Evidence: pytest `test_repeated_request_is_idempotent`; caller scenario 3 ("server recorded N attempts for N sessions").

## A5. Rate limiting
Endpoint `POST /api/v1/auth/login`; limit **5 requests / 60 s sliding window**, shared by **all callers from one client IP**;
excess → `429` + `Retry-After: <seconds until oldest hit leaves the window>`; allowed again at that moment.
Evidence: `scripts/rate_limit_demo.py` output [paste]. (Run it with a fresh window; other scripts that log in count too.)

## A6. Test results
Valid / invalid / repeated request: [paste `pytest -q` output + curl -i examples].

## A7. Python caller
Settings: connect timeout 1 s, read timeout 2 s; **2 retries** (3 attempts), full-jitter backoff base 0.2 s cap 2 s,
retry only on timeout/connection error/502/503/504 and only for the idempotent PUT; breaker opens after **3**
consecutive failed attempts, **5 s** recovery, one half-open probe.
Output: [paste `python scripts/http_caller.py`] — shows success, retry→success, timeout→retry→no duplicate,
breaker OPEN → calls blocked → HALF_OPEN probe → CLOSED. Rate limiting is demonstrated separately (A5).

## B. gRPC
`.proto`: `proto/veritas/exam/v1/exam.proto`; generation: `./scripts/gen_proto.sh`.
Call paths (draw both):
```
HTTP : client ─HTTP/1.1 PUT─▶ uvicorn ─▶ FastAPI route (validation) ─▶ app.core.exam.submit_attempt ─▶ SQLite(/data)
gRPC : client ─▶ ExamServiceStub.SubmitExam (generated) ─HTTP/2─▶ grpc.server ─▶ MetricsInterceptor
        ─▶ ExamService.SubmitExam (handler) ─▶ app.core.exam.submit_attempt ─▶ SQLite(/data)   ← same function, same DB
```
Examples (one success + one failure each): [paste `scripts/demo_traffic.py` rows + status mapping table from README-A2].
Trace correlation: [Kibana screenshot of one `trace.id` showing both service.name values].

## C. Metrics
Audit (A1) — useful: submissions counter, grading histogram, process/host metrics. Gaps found and what was done:
| Gap in A1 | Fix |
|---|---|
| No HTTP request counter with status → no error rate per route | `veritas_http_requests_total{method,route,status}` |
| Only a Summary for latency (no percentiles, can't aggregate) | `veritas_http_request_duration_seconds` histogram |
| `active_test_takers` inc/dec leaked and cannot work across 2 processes; `registered_users` reset on restart | DB-derived collector (open sessions younger than TTL; users counted from DB) |
| Unmatched URLs used the raw path as `route` label (cardinality risk) | label `unmatched` |
| `demo_requests_total{request_id}` ON by default | default OFF (`CARDINALITY_DEMO_USE_LABEL=false`) |
| No visibility of rate limiting / retries absorbed | `veritas_rate_limited_total{endpoint}`, `veritas_idempotent_replays_total{operation}` |
| `quiz_id` label includes UUIDs for admin-created quizzes | bounded in practice (few quizzes); noted, not user/request/trace IDs |

New metrics (fill purpose/type/unit/labels/location/query):
| Metric | Type | Unit | Labels | Code | Query |
|---|---|---|---|---|---|
| `veritas_grpc_server_handled_total` | Counter | calls | grpc_method, grpc_code | `grpc_service/server.py` MetricsInterceptor | `sum by (grpc_method,grpc_code)(rate(…[1m]))` |
| `veritas_grpc_server_handling_seconds` | Histogram | seconds | grpc_method | same | `histogram_quantile(0.95, sum by (le)(rate(…_bucket[5m])))` |
| `veritas_grpc_server_in_flight` | Gauge | calls | grpc_method | same | `sum by (grpc_method)(veritas_grpc_server_in_flight)` |
| `veritas_http_requests_total` | Counter | requests | method, route, status | `app/main.py` middleware | see dashboard |
| `veritas_http_request_duration_seconds` | Histogram | seconds | method, route | same | see dashboard |
| `veritas_http_requests_in_flight` | Gauge | requests | – | same | `veritas_http_requests_in_flight` |
| `veritas_rate_limited_total` | Counter | requests | endpoint | `login_rate_limit` | `rate(…[1m])` |
| `veritas_idempotent_replays_total` | Counter | requests | operation | `app/core/exam.py` | `rate(…[1m])` |
| `veritas_active_test_takers` (redefined) | Gauge (collector) | sessions | quiz_id | `app/core/metrics.py` | `veritas_active_test_takers` |

Duration is measured **at the server** (handler only for gRPC — excludes network and (de)serialization; whole request
for HTTP middleware). Buckets 5 ms … 5 s (grading is ms-scale; the injected 500 ms anomaly lands in the 0.5–0.75 bucket).
p50/p95 over a **5 m** `rate()` window. In-flight chosen over message size: payloads are tiny and bounded, while in-flight
shows saturation of the 10-thread pool and the 500 ms anomaly. Dashboard evidence: [screenshots after
`python scripts/demo_traffic.py --rounds 10 --anomaly`].
