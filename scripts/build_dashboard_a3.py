"""Generates monitoring/grafana/provisioning/dashboards/veritas-a3-load-balancing.json
One dashboard, reused in Parts B, C and D. HTTP = GET /api/v1/quizzes/{quiz_id}, gRPC = ExamService.GetQuiz.
All per-replica series come from scraping A, B, C directly (label `replica` added in prometheus.yml). Window: 1m."""
import json
import os

DS = {"type": "prometheus", "uid": "veritas-prometheus"}
R = "/api/v1/quizzes/{quiz_id}"
panels, _id = [], [0]


def nid():
    _id[0] += 1
    return _id[0]


def row(title, y):
    panels.append({"id": nid(), "type": "row", "title": title, "collapsed": False, "panels": [],
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}})


def ts(title, targets, x, y, unit="short", desc="", w=8, h=8, vmin=None, vmax=None):
    custom = {"drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 2, "fillOpacity": 10,
              "showPoints": "never", "spanNulls": False}
    defaults = {"unit": unit, "custom": custom}
    if vmin is not None:
        defaults["min"] = vmin
    if vmax is not None:
        defaults["max"] = vmax
    panels.append({
        "id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": e, "legendFormat": l, "refId": chr(65 + i), "datasource": DS}
                    for i, (e, l) in enumerate(targets)],
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": defaults, "overrides": []}})


def six(prefix, y, rate, inflight, bucket, errors, extra_desc=""):
    """rate / share / active, then p50 / p95 / errors-by-status (3 per line, 8 wide)."""
    ts(f"{prefix} request rate per replica", [(f"sum by (replica) (rate({rate}[1m]))", "{{replica}}")], 0, y, "reqps",
       "rate(...[1m]) per replica, scraped directly from each replica.")
    ts(f"{prefix} share of total traffic (%)",
       [(f"100 * sum by (replica) (rate({rate}[1m])) / scalar(sum(rate({rate}[1m])))", "{{replica}}")], 8, y,
       "percent", "Replica rate / total rate. Round robin target: 33.3% each (50/25/25 for weights 2:1:1).",
       vmin=0, vmax=100)
    ts(f"{prefix} active requests per replica", [(inflight, "{{replica}}")], 16, y, "short",
       extra_desc or "Requests currently being handled (gauge).")
    ts(f"{prefix} p50 latency per replica",
       [(f"histogram_quantile(0.5, sum by (le, replica) (rate({bucket}[1m])))", "{{replica}}")], 0, y + 8, "s",
       "Server-side, histogram_quantile over a 1m rate() window (includes any injected delay).")
    ts(f"{prefix} p95 latency per replica",
       [(f"histogram_quantile(0.95, sum by (le, replica) (rate({bucket}[1m])))", "{{replica}}")], 8, y + 8, "s",
       "Same, 95th percentile; 1m window.")
    ts(f"{prefix} errors by status per replica", errors, 16, y + 8, "reqps",
       "Non-success responses only; empty = no errors. (A stopped replica has no series: see Envoy row.)")


row("HTTP  GET /api/v1/quizzes/{quiz_id}  (per replica)", 0)
H = f'veritas_http_requests_total{{route="{R}"}}'
six("HTTP", 1, H, "veritas_http_requests_in_flight",
    f'veritas_http_request_duration_seconds_bucket{{route="{R}"}}',
    [(f'sum by (replica, status) (rate(veritas_http_requests_total{{route="{R}",status!~"2.."}}[1m]))',
      "{{replica}} {{status}}")],
    "All HTTP requests in flight on that replica (the test route is the only traffic).")

row("gRPC  ExamService/GetQuiz  (per replica)", 17)
G = 'veritas_grpc_server_handled_total{grpc_method="GetQuiz"}'
six("gRPC", 18, G, 'veritas_grpc_server_in_flight{grpc_method="GetQuiz"}',
    'veritas_grpc_server_handling_seconds_bucket{grpc_method="GetQuiz"}',
    [('sum by (replica, grpc_code) (rate(veritas_grpc_server_handled_total{grpc_method="GetQuiz",grpc_code!="OK"}[1m]))',
      "{{replica}} {{grpc_code}}")])

row("Envoy (scraped directly on :9901)", 34)
C = 'envoy_cluster_name=~"http_backend|grpc_backend"'
ts("Envoy upstream requests/s by cluster", [(f"sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_total{{{C}}}[1m]))",
   "{{envoy_cluster_name}}")], 0, 35, "reqps", "Requests Envoy sent upstream, per cluster.")
ts("Envoy upstream responses by class", [(f"sum by (envoy_cluster_name, envoy_response_code_class) (rate(envoy_cluster_upstream_rq_xx{{{C}}}[1m]))",
   "{{envoy_cluster_name}} {{envoy_response_code_class}}xx")], 8, 35, "reqps",
   "Responses Envoy saw from replicas (5xx incl. connect failures while a replica is down but not yet removed).")
ts("Envoy healthy members", [(f"envoy_cluster_membership_healthy{{{C}}}", "{{envoy_cluster_name}} healthy"),
   (f"envoy_cluster_membership_total{{{C}}}", "{{envoy_cluster_name}} total")], 16, 35, "short",
   "Hosts Envoy currently sends traffic to (active health checking).")
ts("Envoy active upstream requests", [(f"sum by (envoy_cluster_name) (envoy_cluster_upstream_rq_active{{{C}}})",
   "{{envoy_cluster_name}}")], 0, 43, "short", "In-flight requests Envoy has open to all replicas.")
ts("Envoy health-check results/s", [(f"sum by (envoy_cluster_name) (rate(envoy_cluster_health_check_success{{{C}}}[1m]))", "{{envoy_cluster_name}} ok"),
   (f"sum by (envoy_cluster_name) (rate(envoy_cluster_health_check_failure{{{C}}}[1m]))", "{{envoy_cluster_name}} failed")],
   8, 43, "ops", "Only non-zero when the `health` scenario is loaded.")
ts("Envoy downstream requests/s by listener", [("sum by (envoy_http_conn_manager_prefix) (rate(envoy_http_downstream_rq_total[1m]))",
   "{{envoy_http_conn_manager_prefix}}")], 16, 43, "reqps", "http_ingress / grpc_ingress = client-facing listeners.")

dash = {"annotations": {"list": []}, "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1, "id": None,
        "links": [], "liveNow": True, "panels": panels, "refresh": "5s", "schemaVersion": 39,
        "tags": ["veritas", "a3", "envoy"], "templating": {"list": []}, "time": {"from": "now-15m", "to": "now"},
        "timepicker": {}, "timezone": "browser", "title": "Veritas A3 Load Balancing", "uid": "veritas-a3-lb",
        "version": 1, "weekStart": ""}
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "monitoring/grafana/provisioning/dashboards/veritas-a3-load-balancing.json")
with open(out, "w") as f:
    json.dump(dash, f, indent=2)
print("wrote", out, len(panels), "panels")
