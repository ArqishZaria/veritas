# Veritas — Observability-Instrumented Online Assessment Platform

Veritas is a lightweight FastAPI exam platform (timed MCQ quizzes, auto-grading,
and browser-tab anti-cheat detection) built as a complete observability
reference implementation: four Prometheus metric types, host metrics via
Node Exporter, ECS-structured JSON logs shipped through Filebeat into the
Elastic Stack, and two embedded chaos/observability experiments.

---

## 1. Architecture

```
 Browser ──▶ Veritas (FastAPI, :8000) ──▶ SQLite (in-memory)
                 │        │
                 │        └── stdout: ECS JSON logs ──▶ Filebeat ──▶ Elasticsearch ──▶ Kibana
                 │
                 └── /metrics ──▶ Prometheus (:9090) ──▶ Grafana (:3000)
                                       ▲
                          Node Exporter (:9100) — host CPU/mem/disk/net
```

| Service        | Port | Purpose                                   |
|----------------|------|--------------------------------------------|
| veritas        | 8000 | The application itself                    |
| prometheus     | 9090 | Metrics scraping & storage                |
| grafana        | 3000 | Dashboards (Prometheus pre-provisioned)   |
| node-exporter  | 9100 | Host system metrics                       |
| elasticsearch  | 9200 | Log storage & search                      |
| kibana         | 5601 | Log exploration UI                        |
| filebeat       | —    | Ships container stdout → Elasticsearch    |

---

## 2. Starting the Stack

**Prerequisites:** Docker + Docker Compose v2. Allocate Docker at least 4 GB
of RAM (Elasticsearch is the heaviest component).

```bash
git clone <this-repo> veritas
cd veritas
docker compose up --build
```

Wait roughly 60–90 seconds on first boot — Elasticsearch needs a few cycles
to become healthy before Filebeat starts shipping logs. Then open:

- **App:** http://localhost:8000
- **Prometheus:** http://localhost:9090
- **Grafana:** http://localhost:3000 (login: `admin` / `admin`, or use it
  anonymously — anonymous viewer access is enabled by default). A **"Veritas
  Overview"** dashboard is auto-provisioned on first boot — no manual panel
  setup required (see §4).
- **Kibana:** http://localhost:5601

To stop everything:
```bash
docker compose down          # keep data
docker compose down -v       # also wipe the Elasticsearch volume
```

---

## 3. Using the App

Every startup seeds 5 demo student accounts and 12 quiz attempts (a
deliberate mix of passed/failed/cheated results across all 3 quizzes) via
the app's real grading/metrics/logging path — so Grafana, Kibana, and the
admin dashboard already have real data the moment the stack comes up,
without you having to click through the UI a dozen times first.

| Username | Password |
|---|---|
| `alice_demo`, `bob_demo`, `carol_demo`, `dave_demo`, `erin_demo` | `demo123` |

Set `SEED_DEMO_DATA=false` in `docker-compose.yml`'s `veritas` service
(and restart) if you'd rather start from a completely empty instance —
useful for a live demo where you want to generate every data point
yourself, on camera, without pre-existing noise. Either way, the seeding
logic lives in `seed_demo_data()` / `DEMO_ATTEMPTS` in `app/main.py` if
you want to change what gets pre-populated.

1. Open http://localhost:8000 and **Register** a new account (or log in
   as one of the demo students above).
2. On the **Quiz Catalog** tab, search or browse quizzes (`Python Fundamentals`,
   `Computer Networks Basics`, `Observability Fundamentals`).
3. Click **Start Quiz** → read the rules modal → **I Understand — Start Exam**.
4. Answer questions before the 3-minute countdown expires. The exam
   auto-submits on timeout.
5. Check the **Attempted Tests** tab for your score history and running
   average. Click **View Answers** on any attempt to see a per-question
   breakdown (your answer vs. the correct one).
6. Use the 🌙/☀️ toggle in the top-right to switch between light (default)
   and dark mode — your choice is remembered in the browser. Use **Log
   out** to end your session and return to the login screen.

### Admin Dashboard

A demo admin account is seeded automatically every time the app starts:

| Username | Password |
|---|---|
| `admin` | `admin123` |

> ⚠️ These are plaintext demo credentials with no real access control
> beyond a single `is_admin` flag (see §9's auth limitations) — change
> this before using the pattern anywhere beyond a local assignment demo.
> Public `/api/register` can never create an admin account; this seeded
> row is the only way in.

Log in as `admin` to reach the admin dashboard instead of the student
view, with three tabs:

- **Overview** — registered student count, quiz count, total attempts,
  and overall cheating rate at a glance, plus direct links to open
  **Grafana** and **Kibana** for the deep-dive metrics and logs.
- **All Results** — every student's every attempt in one table, with a
  **"Show only flagged cheating"** checkbox to isolate cheating incidents.
- **Manage Quizzes** — see existing quizzes (with delete), and a form to
  add a new quiz: title, topic, duration, and a dynamic question builder
  (each question gets 4 options and a radio button marking the correct
  one). New quizzes appear in the student catalog immediately.

### Testing the Anti-Cheat Tab-Switching Penalty

1. Start any quiz.
2. While the exam modal is open, switch to another browser tab, minimize the
   window, or `Alt+Tab` to another application. The Page Visibility API
   (`visibilitychange`/`blur`/`focus` listeners in `index.html`) detects this
   immediately and:
   - Shows a red warning banner in the exam UI.
   - POSTs to `/api/exam/tab_switch`, which emits a `"warn"`-level ECS log
     event with `event.action: "tab_switched"`.
   - Increments the tab-switch counter **exactly once per departure**, no
     matter how many of the underlying browser events fire for that one
     departure (see the code comment above `handleVisibilityChange` in
     `index.html` for why this needed an explicit guard).
3. Submit the exam (or let the timer expire). Because `tab_switch_count > 0`,
   the backend forces `final_score = 0` and records `status: "cheated"`.
4. Confirm on the **Attempted Tests** tab: the attempt shows a `cheated`
   badge with score `0%` regardless of how many questions were answered
   correctly.
5. Cross-check in Kibana (see §5) by filtering
   `event.action: "tab_switched"` or `event.action: "quiz_submitted" AND status: "cheated"`.
   Or check it as an admin: log in as `admin` → **All Results** → tick
   **"Show only flagged cheating."**

---

## 4. Grafana / PromQL — Dashboards

Both the Prometheus datasource **and a fully built "Veritas Overview"
dashboard** are auto-provisioned (`monitoring/grafana/provisioning/`) —
open Grafana, go to **Dashboards**, and it's already there with live data.
No manual panel setup is required. It's organized into four rows:

| Row | Panels |
|---|---|
| **Overview** | Total Submissions, Cheating Rate %, Registered Users, Active Test-Takers (stat panels) |
| **Business Metrics** | Submission Rate by Status, Active Test-Takers by Quiz |
| **Latency (Histogram + Summary)** | Grading Duration p95/p99, Gateway Avg Processing Time by Route |
| **Application Process Health** | Veritas Process CPU, Veritas Process Memory (RSS) |
| **Host System (Node Exporter)** | Host CPU %, Host Memory %, Host Disk I/O, Host Network I/O |

The queries behind each panel (also usable directly in **Explore**):

**Submission throughput (per status), rate over 1 min:**
```promql
sum(rate(veritas_quiz_submissions_total[1m])) by (status)
```

**Currently active test-takers, per quiz:**
```promql
veritas_active_test_takers
```

**p95 / p99 grading latency (histogram_quantile):**
```promql
histogram_quantile(0.95, sum(rate(veritas_grading_duration_seconds_bucket[5m])) by (le, quiz_id))
histogram_quantile(0.99, sum(rate(veritas_grading_duration_seconds_bucket[5m])) by (le, quiz_id))
```

**Gateway processing time — average from the Summary (`_sum` / `_count`):**
```promql
rate(veritas_gateway_processing_seconds_sum[5m]) / rate(veritas_gateway_processing_seconds_count[5m])
```
> ⚠️ **Important caveat about the Summary metric type:** the *Python*
> `prometheus_client` library's `Summary` only tracks a running `_sum` and
> `_count` — it does **not** expose `{quantile="0.5"}`-style series the way
> the Java client does. If you query
> `veritas_gateway_processing_seconds{quantile="0.5"}` you will get no
> data — that series doesn't exist in this stack. The query above (dividing
> the rate of `_sum` by the rate of `_count`) is the correct way to get a
> genuine sliding-window *average* out of a Python Summary. For real
> **percentiles** (p95/p99), use the Histogram instrument with
> `histogram_quantile()` as shown above — that's exactly why this project
> uses both instrument types rather than relying on the Summary alone.

**Veritas process CPU / memory (free via the default Prometheus registry):**
```promql
rate(process_cpu_seconds_total{job="veritas-app"}[1m])
process_resident_memory_bytes{job="veritas-app"}
```
These come from `prometheus_client`'s built-in process/platform/GC
collectors, which register automatically as long as the app uses the
*default* global registry (this project does) rather than a fresh
`CollectorRegistry()`.

**Registered users / bonus Info metric:**
```promql
veritas_registered_users_total
veritas_app_info
```

**Host CPU usage (from Node Exporter):**
```promql
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

**Host memory usage %:**
```promql
100 * (1 - ((node_memory_MemAvailable_bytes) / (node_memory_MemTotal_bytes)))
```

**Host disk / network I/O:**
```promql
rate(node_disk_read_bytes_total[5m])
rate(node_disk_written_bytes_total[5m])
rate(node_network_receive_bytes_total{device!="lo"}[5m])
rate(node_network_transmit_bytes_total{device!="lo"}[5m])
```

---

## 5. Kibana / KQL — Log Queries

1. In Kibana, go to **Stack Management → Data Views** and create a data view
   matching `veritas-logs-*` (timestamp field: `@timestamp`). This only needs
   to be done once.
2. Go to **Discover**, select the `veritas-logs-*` data view, and try:

**All tab-switch (potential cheating) events:**
```
service.name: "veritas-app" AND event.action: "tab_switched"
```

**Confirmed cheating penalties:**
```
event.action: "quiz_submitted" AND status: "cheated"
```

**Failed login attempts:**
```
event.action: "login_failed"
```

**Errors and warnings only:**
```
service.name: "veritas-app" AND log.level: ("warn" or "error")
```

**Correlate a specific request end-to-end by trace ID:**
```
trace.id: "<paste a trace.id value from any log line>"
```

**All events for a specific student:**
```
student.id: "<uuid>"
```

---

## 6. Part E — Embedded Observability Experiments

### 6.1 Anomaly Injection (artificial latency spike)

Two ways to trigger it:

- **Manual:** append `?simulate_anomaly=true` to the submit request — easiest
  way to test is from the browser console while an exam is open:
  ```js
  fetch('/api/exam/submit?simulate_anomaly=true', { ... })
  ```
- **Automatic:** the backend injects a 500 ms `asyncio.sleep(0.5)` delay on
  **every 5th submission**, regardless of the query flag (see
  `_submission_counter` logic in `app/main.py`).

**To observe it:**
1. Submit at least 5 exams in a row (any account/quiz combination).
2. In Grafana, watch the p95 query from §4 — you'll see a visible latency
   spike coinciding with the 5th, 10th, 15th... submission.
3. In Kibana, filter:
   ```
   event.action: "anomaly_injected"
   ```
   Each injected delay emits a `"warn"` log with `delay_ms: 500`, letting you
   correlate the Grafana spike with the exact request in Kibana via its
   `trace.id`.

### 6.2 Cardinality Explosion Demonstration

`app/main.py` contains a commented-out `BAD_CARDINALITY_COUNTER` example
(search for `PART E.2` in the file) that attaches `student_id` as a
Prometheus label alongside `status` and `quiz_id`.

**To run the demonstration:**
1. Open `app/main.py` and uncomment the `BAD_CARDINALITY_COUNTER` definition
   and add a `.labels(status=status, quiz_id=quiz_id, student_id=user_id).inc()`
   call next to the existing safe counter's `.inc()` call in `submit_exam()`.
2. Rebuild and register/submit exams as 10–20 *different* student accounts.
3. In Prometheus, compare:
   ```promql
   count(veritas_quiz_submissions_total)                     # bounded: ~ status × quiz_id combinations
   count(veritas_quiz_submissions_by_student_total)           # grows with every new student
   ```
4. The first query stays flat (at most `3 statuses × 3 quizzes = 9` series).
   The second grows linearly and unboundedly with user signups — this is the
   time-series explosion anti-pattern. Revert the change afterward; this
   counter should never run in a real deployment.
5. **Takeaway:** high-cardinality identifiers (student IDs, request UUIDs,
   raw IPs, emails) belong in **logs** (queryable per-event via
   `student.id` in Kibana, §5) — never as **metric labels**, which are meant
   for a small, bounded set of aggregate dimensions.

---

## 7. Project Structure

```
veritas/
├── app/
│   ├── main.py                  # FastAPI backend, metrics, ECS logger
│   └── templates/index.html     # Frontend: auth, catalog, exam modal, anti-cheat
├── monitoring/
│   ├── prometheus/prometheus.yml
│   ├── filebeat/filebeat.yml
│   └── grafana/provisioning/
│       ├── datasources/datasource.yml     # Pre-wired Prometheus datasource
│       └── dashboards/
│           ├── dashboards.yml             # Tells Grafana where to load dashboards from
│           └── veritas-overview.json      # The pre-built "Veritas Overview" dashboard
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 8. Troubleshooting

**Elasticsearch container exits immediately / logs "max virtual memory
areas vm.max_map_count [65530] is too low":**
This is a well-known Elasticsearch-on-Docker requirement on Linux hosts
(usually not needed on Docker Desktop for Mac/Windows, which sets it for
you). Fix it on the host, then restart the stack:
```bash
sudo sysctl -w vm.max_map_count=262144
# to make it permanent:
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
```

**Kibana shows "Kibana server is not ready yet" or can't reach
Elasticsearch on first boot:**
`docker-compose.yml` already makes `kibana` and `filebeat` wait for
Elasticsearch's healthcheck (`condition: service_healthy`) before starting,
and `prometheus` waits for `veritas`'s healthcheck the same way. On a slow
machine, Elasticsearch can still take up to a minute to pass its
healthcheck the very first time (JVM warm-up + index initialization) — give
it a minute and refresh.

**No data appears in Grafana / the "Veritas Overview" dashboard is empty:**
Interact with the app first (register a user, take a quiz) — Prometheus
only has data once `/metrics` has been scraped at least once *after* an
event occurred. Confirm scraping is healthy at
http://localhost:9090/targets — both `veritas-app` and `node-exporter`
should show as `UP`.

**No logs appear in Kibana:**
1. Confirm the `veritas-logs-*` data view exists (Stack Management → Data
   Views) — this is a one-time manual step, not automated by this project.
2. Confirm Filebeat is actually running and not stuck waiting on
   Elasticsearch: `docker compose logs filebeat`.
3. Docker socket access can be blocked by SELinux/AppArmor on some Linux
   distros — check `docker compose logs filebeat` for permission errors
   against `/var/run/docker.sock`.

**Building on Apple Silicon / ARM:**
The Dockerfile's build stage includes `build-essential` specifically so
that any dependency without a prebuilt `arm64` wheel can still compile from
source. If a build is unusually slow the first time, that's why — it only
happens once, since Docker layer-caches the result.

---

## 9. Notes & Known Limitations

- **Auth uses plaintext password storage and an in-memory SQLite DB** —
  this is a teaching/demo artifact for the observability assignment, **not**
  production-hardened auth. Swap in password hashing (bcrypt/argon2) and a
  persistent database for real deployments.
- **The in-memory SQLite DB resets whenever the `veritas` container
  restarts** — expected and by design, for a stateless assignment demo.
- **`veritas_active_test_takers` can "leak" upward** if a student starts an
  exam and closes the tab/browser without submitting (no corresponding
  `.dec()` call ever fires). This is intentional simplicity for the
  assignment scope; a production version would add a TTL-based expiry or a
  periodic reconciliation job.
- **The anti-cheat `blur` listener is intentionally strict**: it fires on
  *any* loss of window focus, including opening browser dev tools or
  responding to an OS-level dialog — not just deliberate tab-switching.
  That's the intended behavior for this assignment's threat model (treat
  any focus loss as suspicious), but it does mean occasional false
  positives are possible in real use. (This is separate from — and not to
  be confused with — an earlier version of this project that double-counted
  each real departure once via `blur` and once via `visibilitychange`;
  that's fixed now via the `isAwayFromExam` guard in `index.html`, so one
  departure is always exactly one count.)
- **The Python `prometheus_client` Summary type does not compute
  quantiles** (see §4's callout) — only the Histogram instrument gives you
  true percentiles here. This is a client-library limitation, not a bug in
  this project, but it's easy to assume otherwise if you've used the Java
  client before.
- **Do not run this with multiple Uvicorn workers** (e.g. `--workers 4`).
  Both the in-memory SQLite database (`file:veritas_db?mode=memory&cache=shared`)
  and the Prometheus client's in-process metric registry are per-process
  state — a second worker process would see an empty database and report
  its own separate (and incomplete) set of metrics, silently splitting your
  data across processes with no error raised. The Dockerfile's `CMD`
  intentionally runs a single worker for this reason; if you need to scale
  this beyond a demo, that means moving to a real database and either a
  shared metrics backend (e.g. `prometheus_client`'s multiprocess mode with
  a shared directory) or per-process scraping with aggregation.
- **Admin access control is the same lightweight `user_id`-as-proof-of-identity
  model as the rest of the app** — there's no real session/token layer, so
  any request that includes an admin's `user_id` is treated as coming from
  that admin. This is consistent with how the whole app already works (see
  the point above about plaintext passwords), not a separate weaker path;
  it's still not something to expose beyond a local demo.
- Elasticsearch runs in single-node, security-disabled mode purely to keep
  the assignment's local setup simple — never do this outside a local demo.
