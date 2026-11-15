"""Generates monitoring/grafana/provisioning/dashboards/veritas-grpc-vs-http.json"""
import json
import os

DS = {"type": "prometheus", "uid": "veritas-prometheus"}
R = "/api/v1/sessions/{session_id}/attempt"
panels, _id = [], [0]


def nid():
    _id[0] += 1
    return _id[0]


def row(title, y):
    panels.append({"id": nid(), "type": "row", "title": title, "collapsed": False, "panels": [],
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}})


def ts(title, targets, x, y, unit="short", w=12, h=8, desc=""):
    panels.append({
        "id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": e, "legendFormat": l, "refId": chr(65 + i), "datasource": DS}
                    for i, (e, l) in enumerate(targets)],
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {
            "drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 2, "fillOpacity": 12,
            "showPoints": "never", "spanNulls": True}}, "overrides": []}})


G = "veritas_grpc_server_handled_total"
H = f'veritas_http_requests_total{{route="{R}"}}'
GB = 'veritas_grpc_server_handling_seconds_bucket{grpc_method="SubmitExam"}'
HB = f'veritas_http_request_duration_seconds_bucket{{route="{R}",method="PUT"}}'


def q(p, b):
    return f"histogram_quantile({p}, sum by (le) (rate({b}[5m])))"


row("Calls & error rate: SubmitExam (gRPC) vs PUT /sessions/{id}/attempt (HTTP)", 0)
ts("gRPC calls/s by method & status code",
   [(f"sum by (grpc_method, grpc_code) (rate({G}[1m]))", "{{grpc_method}} {{grpc_code}}")], 0, 1, "reqps")
ts("HTTP calls/s by method & status code (same operation)",
   [(f"sum by (method, status) (rate({H}[1m]))", "{{method}} {{status}}")], 12, 1, "reqps")
ts("gRPC error rate % (non-OK / all, 5m)",
   [(f'100 * sum(rate({G}{{grpc_code!="OK"}}[5m])) / sum(rate({G}[5m]))', "gRPC non-OK %")], 0, 9, "percent",
   desc="Includes client errors (NOT_FOUND, INVALID_ARGUMENT, ...) and server errors.")
ts("HTTP error rate % (status >= 400 / all, 5m)",
   [(f'100 * sum(rate(veritas_http_requests_total{{route="{R}",status=~"4..|5.."}}[5m])) / sum(rate({H}[5m]))',
     "HTTP 4xx+5xx %")], 12, 9, "percent")

row("Latency (histogram, 5m window)", 17)
ts("gRPC SubmitExam p50 / p95 (server-side)", [(q(0.5, GB), "p50"), (q(0.95, GB), "p95")], 0, 18, "s",
   desc="histogram_quantile over rate(...[5m]); measured at the server handler.")
ts("HTTP PUT attempt p50 / p95 (server-side)", [(q(0.5, HB), "p50"), (q(0.95, HB), "p95")], 12, 18, "s")
ts("p95 compared: gRPC vs HTTP", [(q(0.95, GB), "gRPC p95"), (q(0.95, HB), "HTTP p95")], 0, 26, "s", w=24, h=7)

row("Saturation: in-flight calls", 33)
ts("gRPC in-flight calls", [("sum by (grpc_method) (veritas_grpc_server_in_flight)", "{{grpc_method}}")], 0, 34)
ts("HTTP in-flight requests (all routes)", [("veritas_http_requests_in_flight", "in flight")], 12, 34)

row("Other metrics added in A2", 42)
ts("Rate-limited requests (429) per second",
   [("sum by (endpoint) (rate(veritas_rate_limited_total[1m]))", "{{endpoint}}")], 0, 43, "reqps")
ts("Idempotent replays per second",
   [("sum by (operation) (rate(veritas_idempotent_replays_total[1m]))", "{{operation}}")], 12, 43, "reqps")
ts("Active test-takers (DB-derived, per quiz)", [("veritas_active_test_takers", "{{quiz_id}}")], 0, 51, w=24, h=7)

dash = {"annotations": {"list": []}, "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1, "id": None,
        "links": [], "liveNow": True, "panels": panels, "refresh": "5s", "schemaVersion": 39,
        "tags": ["veritas", "grpc", "a2"], "templating": {"list": []}, "time": {"from": "now-15m", "to": "now"},
        "timepicker": {}, "timezone": "browser", "title": "Veritas gRPC vs HTTP", "uid": "veritas-grpc-http",
        "version": 1, "weekStart": ""}
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "monitoring/grafana/provisioning/dashboards/veritas-grpc-vs-http.json")
with open(out, "w") as f:
    json.dump(dash, f, indent=2)
print("wrote", out, len(panels), "panels")
