# Assignment 3 report — skeleton (fill the brackets from YOUR runs; write explanations in your own words)

## 1. Architecture and shared state (Part A)
[paste/draw the diagram from README-A3.md]. Replicas A, B, C, each HTTP :8000 + gRPC :50051 in one process; Envoy HTTP listener
(HTTP connection manager) and gRPC listener (HTTP/2 connection manager, per-call routing); fixed replica IPs, STATIC endpoints.
Test operations: HTTP `GET /api/v1/quizzes/{id}`, gRPC `ExamService/GetQuiz` (read-only, added in A3).
| State | Shared / separate | What would go wrong if separate |
|---|---|---|
| Data | shared SQLite (WAL) on one volume | [your words] |
| Idempotency records | shared: `attempts.UNIQUE(session_id)` in the same DB | [your words] |
| Rate-limit counters | A2: in memory (separate) → A3: shared table `rate_limit_hits` | [e.g. 3 × the limit, restart resets] |
Replica id: logs `replica.id`; metrics `replica` label (scrape config); `X-Replica-Id`; trace id forwarded unchanged.

## 2. Metrics, queries, health checks
Window: 1 m `rate()` for all p50/p95 (`histogram_quantile(q, sum by (le, replica)(rate(<bucket>[1m])))`); share = replica rate / total rate;
active = `veritas_http_requests_in_flight` / `veritas_grpc_server_in_flight{grpc_method="GetQuiz"}`; errors = non-2xx / non-OK by status.
Prometheus scrapes A, B, C (`veritas-x:8000`) and Envoy (`envoy:9901`) directly. Labels bounded (no unique ids).
Health checks (`health` scenario): HTTP `GET /health`, interval 2 s, timeout 1 s, unhealthy threshold 2, healthy threshold 2
(gRPC cluster checks the HTTP port of the same process via `alt_port`). `/health` is never delayed — so a slow replica still passes.

## 3. Baselines (Parts B and C)
Workload: [rate] rps for [duration] s, open loop, [HTTP|gRPC]; no retries/breakers.
| Run | Target / achieved rps | Sent | Success | Failed | Client p95 | A/B/C share | Notes |
|---|---|---|---|---|---|---|---|
| B baseline HTTP (A only) | | | | | | | |
| B baseline gRPC (A only) | | | | | | | |
| C round robin HTTP | | | | | | | difference from 33%: [explain] |
| C round robin gRPC (HTTP/2 listener) | | | | | | | [explain per-call vs per-connection] |

## 4. Experiments (Part D) — prediction → observation → explanation, HTTP and gRPC separately
D1 weighted 2:1:1 (vs 50/25/25) · D2 least request vs round robin with B +[delay] ms · D3 stop/start B.
[charts + client JSON for each]. D3: removal took [x] s, recovery [y] s ↔ settings (interval 2 s × threshold 2; plus app start-up time); client errors [n].

## 5. Summary table (all runs)
| Run / interface | Policy and fault | Target / actual rps | A/B/C shares | Client p95 | Error rate | Explanation |
|---|---|---|---|---|---|---|

## 6. Proxy and backend log examples (one per interface)
HTTP: Kibana `trace.id : "<id>"` → [Envoy event + replica event, same `replica.id`]. gRPC: [same].
Distribution cross-check: `envoy_log_shares.py` for [time range] gave [counts/shares] vs client/dashboard [counts/shares].

## 7. Conclusions
1. Strategy for HTTP / for gRPC and why (use your numbers). 2. One workload where you would choose differently.
3. If Envoy stops: [what happens]; one improvement (e.g. two Envoy instances + a virtual IP / DNS or an external load balancer).
