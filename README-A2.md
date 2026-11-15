# Assignment 2 — HTTP API, gRPC, Metrics (additions to the A1 README)

## What changed (one paragraph)
`app/main.py` was split so the business rules live in `app/core/exam.py` and are called by **both** the
HTTP handlers and the gRPC handler. Both processes share one SQLite file on the `veritasdata` volume
(WAL mode, `BEGIN IMMEDIATE` writers). The API moved to `/api/v1`, with one error format, an idempotent
submit (`PUT /sessions/{id}/attempt`), a rate-limited login, and a generated OpenAPI spec. A new container
`veritas-grpc` serves `ExamService.SubmitExam` and exposes metrics on `:9101`.

## Apply it to your repo
```bash
# copy every file from this package over your repo (same paths), then:
python scripts/patch_frontend.py          # updates app/templates/index.html to /api/v1 (asserts every edit)
docker compose down -v                    # the DB is now a persistent file; start clean once
docker compose up --build
```
Stack ports: app 8000 · gRPC 50051 · gRPC metrics 9101 · Prometheus 9090 · Grafana 3000 · Kibana 5601.
Grafana dashboards: **Veritas Overview** (A1) and **Veritas gRPC vs HTTP** (A2, auto-provisioned).

## Run & test
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
./scripts/gen_proto.sh                    # generation command -> gen/veritas/exam/v1/*_pb2*.py
python -m pytest -q                       # 14 tests: API, idempotency, 429, OpenAPI drift, gRPC, metrics
python scripts/export_openapi.py          # regenerate openapi/openapi.json after changing routes/models
```
| Goal | Command |
|---|---|
| Inspect/test the spec | http://localhost:8000/docs (Swagger UI → *Try it out*), spec file `openapi/openapi.json` |
| Resilience demo (timeout, retry, breaker) | `python scripts/http_caller.py` |
| Rate-limit demo (429 + Retry-After) | `python scripts/rate_limit_demo.py` (run when the 60 s window is fresh) |
| gRPC call by hand | `python scripts/grpc_client.py --session-id <id> --user-id <id> --answers 0=1,1=1,2=0,3=2,4=1 --deadline 3` |
| Repeatable traffic for Grafana (HTTP + gRPC, success + failure) | `python scripts/demo_traffic.py --rounds 10` (add `--anomaly` for a latency spike) |
| Background load | `python scripts/load_generator.py --users 10 --duration 120` |

Get a session id by hand:
```bash
UID=$(curl -s -X POST localhost:8000/api/v1/auth/register -H 'content-type: application/json' \
      -d '{"username":"me_test","password":"secret123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["user_id"])')
SID=$(curl -s -X POST localhost:8000/api/v1/quizzes/python_101/sessions -H 'content-type: application/json' \
      -d "{\"user_id\":\"$UID\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -i -X PUT localhost:8000/api/v1/sessions/$SID/attempt -H 'content-type: application/json' \
     -d "{\"user_id\":\"$UID\",\"answers\":{\"0\":1,\"1\":1,\"2\":0,\"3\":2,\"4\":1}}"   # 201; repeat it -> 200 + Idempotent-Replay
```

## Endpoint map (A1 -> A2)
| A1 | A2 | Status codes |
|---|---|---|
| `POST /api/register` | `POST /api/v1/auth/register` | 201, 409, 422 |
| `POST /api/login` | `POST /api/v1/auth/login` (5 / 60 s per IP) | 200, 401, 422, **429** |
| `GET /api/quizzes[?search]`, `GET /api/quizzes/{id}` | `GET /api/v1/quizzes`, `/quizzes/{id}` | 200, 404 |
| `POST /api/exam/start` | `POST /api/v1/quizzes/{id}/sessions` | 201 (+`Location`), 404 |
| `POST /api/exam/tab_switch` | `POST /api/v1/sessions/{id}/tab-switches` | 201, 403, 404, 409 |
| `POST /api/exam/submit` | **`PUT /api/v1/sessions/{id}/attempt`** | 201 first, **200 replay**, 403, 404, 409, 422 |
| `GET /api/history/{uid}` | `GET /api/v1/users/{uid}/attempts` | 200, 404 |
| `GET /api/attempts/{id}/review` | `GET /api/v1/attempts/{id}?user_id=` | 200, 403, 404 |
| `/api/admin/*` | `/api/v1/admin/*` (create 201+`Location`, delete 204) | 200/201/204, 403, 404, 422 |
| `POST /api/experiments/cardinality` | `POST /api/v1/experiments/cardinality` | 200 |

Error shape everywhere: `{"error": {"code", "message", "trace_id", "details"?}}`.

## HTTP ↔ gRPC status mapping (SubmitExam)
| Situation | HTTP | gRPC |
|---|---|---|
| First submit | 201 Created | `OK` (`replayed=false`) |
| Repeat, same answers | 200 + `Idempotent-Replay: true` | `OK` (`replayed=true`) |
| Unknown session | 404 `not_found` | `NOT_FOUND` |
| Session of another user | 403 `forbidden` | `PERMISSION_DENIED` |
| Option/question index out of range, malformed body | 422 | `INVALID_ARGUMENT` |
| Repeat with *different* answers | 409 `conflict` | `ALREADY_EXISTS` |
| Injected outage | 503 | `UNAVAILABLE` (not injected on gRPC) / `INTERNAL` for bugs |
| Client deadline passed | client timeout | `DEADLINE_EXCEEDED` (raised by the client library) |

## Trace correlation across the boundary
Clients send `X-Trace-Id` (HTTP header) or `x-trace-id` (gRPC metadata); servers adopt it when it is 32 hex
chars, otherwise create one, and echo it back (`X-Trace-Id` header / gRPC trailing metadata). In
`scripts/demo_traffic.py` one trace id spans an HTTP session start **and** the gRPC submit. In Kibana:
`trace.id: "<id>"` returns docs from both `service.name: "veritas-app"` and `"veritas-grpc"`.

## Notes / limits
* One SQLite file shared by two containers is fine for a demo (WAL + busy_timeout). For real scale use Postgres.
* `ENABLE_FAULT_INJECTION=true` exists only for the resilience demo; the hook is not in the OpenAPI spec.
* Rate-limit state is in-process (single instance); behind a proxy you would key on a trusted `X-Forwarded-For`.
* Auth is still the A1 "user_id as identity" model — unchanged, still demo-only.
