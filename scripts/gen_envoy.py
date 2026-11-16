#!/usr/bin/env python3
"""Generates envoy/<scenario>.yaml (static config; Envoy has no env substitution, so one file per scenario).
Scenarios: a-only | rr | weighted | least-request | health.   Switch with: python scripts/scenario.py <name>
Replicas have fixed IPs on veritas-net (see docker-compose.yml) so a stopped replica stays a known, STATIC endpoint:
removal/recovery is then decided by the active health checker only, not by Docker DNS."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IPS = {"A": "172.28.0.11", "B": "172.28.0.12", "C": "172.28.0.13"}
APP_PORT, GRPC_APP_PORT = 8000, 50051          # inside each replica container
HTTP_LISTEN, GRPC_LISTEN = 8080, 50051          # Envoy listeners (host: 8000 and 50051)
ALL = ["A", "B", "C"]
SCENARIOS = {
    "a-only":        dict(members=["A"], weights={"A": 1}, policy="ROUND_ROBIN", hc=False),
    "rr":            dict(members=ALL, weights={"A": 1, "B": 1, "C": 1}, policy="ROUND_ROBIN", hc=False),
    "weighted":      dict(members=ALL, weights={"A": 2, "B": 1, "C": 1}, policy="ROUND_ROBIN", hc=False),
    "least-request": dict(members=ALL, weights={"A": 1, "B": 1, "C": 1}, policy="LEAST_REQUEST", hc=False),
    "health":        dict(members=ALL, weights={"A": 1, "B": 1, "C": 1}, policy="ROUND_ROBIN", hc=True),
}
# Active health check (recorded in the report): interval 2s, timeout 1s, unhealthy 2, healthy 2.
HC = """    health_checks:
    - timeout: 1s
      interval: 2s
      interval_jitter: 0s
      no_traffic_interval: 2s
      unhealthy_interval: 2s
      unhealthy_threshold: 2
      healthy_threshold: 2
      http_health_check: {path: /health, codec_client_type: HTTP1}   # uvicorn speaks HTTP/1.1 even on the gRPC cluster (h2 upstream)
@@ALT@@"""

ACCESS_LOG = """          access_log:
          - name: envoy.access_loggers.stdout
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog
              log_format:
                json_format:
                  "@timestamp": "%START_TIME(%Y-%m-%dT%H:%M:%S.%3fZ)%"
                  "message": "envoy access"
                  "service.name": "veritas-envoy"
                  "interface": "@@IFACE@@"
                  "trace.id": "%REQ(X-TRACE-ID)%"
                  "replica.id": "%RESP(X-REPLICA-ID)%"
                  "upstream.host": "%UPSTREAM_HOST%"
                  "upstream.cluster": "%UPSTREAM_CLUSTER%"
                  "http.request.method": "%REQ(:METHOD)%"
                  "url.path": "%REQ(:PATH)%"
                  "http.response.status_code": "%RESPONSE_CODE%"
                  "grpc.status": "%GRPC_STATUS%"
                  "envoy.duration_ms": "%DURATION%"
                  "envoy.response_flags": "%RESPONSE_FLAGS%\""""

LISTENER = """  - name: @@IFACE@@_listener
    address:
      socket_address: {address: 0.0.0.0, port_value: @@PORT@@}
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: @@IFACE@@_ingress
          codec_type: AUTO   # gRPC listener: HTTP/2 connection manager (NOT tcp_proxy) => routing is per call
          use_remote_address: true
@@ACCESS_LOG@@
          route_config:
            name: @@IFACE@@_routes
            virtual_hosts:
            - name: veritas
              domains: ["*"]
              routes:
@@ROUTES@@
          http_filters:
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
"""
HTTP_ROUTES = """              - match: {prefix: "/internal"}
                direct_response: {status: 404, body: {inline_string: "not routed through the balancer"}}
              - match: {prefix: "/"}
                route: {cluster: http_backend, timeout: 30s}   # no retry_policy => no retries"""
GRPC_ROUTES = """              - match: {prefix: "/veritas.exam.v1.ExamService/"}
                route: {cluster: grpc_backend, timeout: 30s}   # no retry_policy => no retries"""

CLUSTER = """  - name: @@NAME@@
    type: STATIC
    connect_timeout: 1s
    lb_policy: @@POLICY@@
@@LR@@    circuit_breakers:        # raised so high that they never trip (A3: breakers disabled during tests)
      thresholds:
      - priority: DEFAULT
        max_connections: 100000
        max_pending_requests: 100000
        max_requests: 100000
        max_retries: 0
@@H2@@@@HC@@    load_assignment:
      cluster_name: @@NAME@@
      endpoints:
      - lb_endpoints:
@@ENDPOINTS@@
"""
H2 = """    typed_extension_protocol_options:
      envoy.extensions.upstreams.http.v3.HttpProtocolOptions:
        "@type": type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions
        explicit_http_config:
          http2_protocol_options: {}
"""


def cluster(name, port, sc, grpc):
    # gRPC replicas share one process with the HTTP server, so the health check targets the HTTP port of the same
    # host (endpoint.health_check_config.port_value; HealthCheck.alt_port is NOT honoured here -- verified with Envoy 1.31).
    hcc = f"\n            health_check_config: {{port_value: {APP_PORT}}}" if (grpc and sc["hc"]) else ""
    eps = "\n".join(f"""        - endpoint:
            address:
              socket_address: {{address: {IPS[r]}, port_value: {port}}}{hcc}
          load_balancing_weight: {sc['weights'][r]}""" for r in sc["members"])
    hc = ""
    if sc["hc"]:
        # gRPC replicas share one process with the HTTP server, so /health on the HTTP port (alt_port) covers both.
        hc = HC.replace("@@ALT@@", "").rstrip("\n") + "\n"
    lr = "    least_request_lb_config: {choice_count: 3}   # look at all 3 replicas, pick the one with fewest active requests\n" \
        if sc["policy"] == "LEAST_REQUEST" else ""
    return (CLUSTER.replace("@@NAME@@", name).replace("@@POLICY@@", sc["policy"]).replace("@@LR@@", lr)
            .replace("@@H2@@", H2 if grpc else "").replace("@@HC@@", hc).replace("@@ENDPOINTS@@", eps))


def render(sc_name):
    sc = SCENARIOS[sc_name]
    out = f"# GENERATED by scripts/gen_envoy.py -- scenario: {sc_name}. Do not edit by hand.\n"
    out += "admin:\n  address:\n    socket_address: {address: 0.0.0.0, port_value: 9901}\n\nstatic_resources:\n  listeners:\n"
    for iface, port, routes in (("http", HTTP_LISTEN, HTTP_ROUTES), ("grpc", GRPC_LISTEN, GRPC_ROUTES)):
        out += (LISTENER.replace("@@ACCESS_LOG@@", ACCESS_LOG).replace("@@IFACE@@", iface)
                .replace("@@PORT@@", str(port)).replace("@@ROUTES@@", routes))
    out += "  clusters:\n"
    out += cluster("http_backend", APP_PORT, sc, grpc=False)
    out += cluster("grpc_backend", GRPC_APP_PORT, sc, grpc=True)
    return out


if __name__ == "__main__":
    for name in SCENARIOS:
        with open(os.path.join(ROOT, "envoy", f"{name}.yaml"), "w") as f:
            f.write(render(name))
        print("wrote envoy/%s.yaml" % name)