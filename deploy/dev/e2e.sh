#!/usr/bin/env bash
# End-to-end checks against the running dev cluster (after `tilt ci` or
# `tilt up`). Each check prints what it verified; any failure exits non-zero.
set -euo pipefail

CONTEXT=kind-iki
NS=iki
kc() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
pass() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
secret() { kc get secret "$1" -o "jsonpath={.data.$2}" | base64 -d; }

PIDS=()
cleanup() { for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

forward() { # forward <target> <local-port> <remote-port>
  kc port-forward "$1" "$2:$3" >/dev/null 2>&1 &
  PIDS+=($!)
  for _ in $(seq 1 50); do
    if nc -z 127.0.0.1 "$2" 2>/dev/null; then return; fi
    sleep 0.2
  done
  fail "port-forward to $1 did not come up"
}

echo "Iki dev cluster end-to-end checks"

# Wait for any rollout in progress, so logs and execs hit the current pods.
kc rollout status deploy/iki-web --timeout=180s >/dev/null

# A port-forward to the Service could land on a pod that is still shutting
# down after the rollout; forward to a ready pod that isn't terminating.
web_pod=$(kc get pods -l app.kubernetes.io/component=web -o json | jq -r '
  [.items[] | select(.metadata.deletionTimestamp == null)
            | select([.status.containerStatuses[]?.ready] | all and length > 0)]
  | .[0].metadata.name // empty')
[ -n "$web_pod" ] || fail "no ready web pod"
forward "pod/$web_pod" 18000 8000
forward svc/keycloak 18080 8080
forward svc/seaweedfs 18333 8333
forward svc/otel-lgtm 13200 3200

# --- Iki web ------------------------------------------------------------------
ready=$(curl -fsS http://127.0.0.1:18000/healthz/ready)
[ "$ready" = '{"status": "ok", "checks": {"database": "ok", "migrations": "ok"}}' ] \
  || fail "readiness: $ready"
pass "web is ready: Postgres reachable over verified TLS, migrations applied"

status=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:18000/e2e-probe?token=e2e-secret")
[ "$status" = 404 ] || fail "unknown path returned $status"
sleep 1

logs=$(kc logs "pod/$web_pod" -c web --since=10m)
echo "$logs" | jq -e -s 'length > 0' >/dev/null || fail "web logs are not JSON lines"
pass "every web log line is JSON"
request=$(echo "$logs" | jq -c 'select(.event == "request" and .path == "/e2e-probe")' | tail -1)
[ -n "$request" ] || fail "no request log line for /e2e-probe"
echo "$logs" | grep -q "e2e-secret" && fail "query string leaked into logs"
pass "request logged without its query string"
trace_id=$(echo "$request" | jq -r .trace_id)
[[ "$trace_id" =~ ^[0-9a-f]{32}$ ]] || fail "request log has no trace_id"

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:13200/api/v2/traces/$trace_id" 2>/dev/null \
      | jq -e '[.. | objects | select(.key? == "service.name") | .value.stringValue] | index("iki-server")' >/dev/null; then
    pass "trace $trace_id reached Tempo through the OTLP collector"
    trace_ok=1
    break
  fi
  sleep 2
done
[ -n "${trace_ok:-}" ] || fail "trace $trace_id never reached Tempo"

# --- Network policy -------------------------------------------------------------
probe() { # probe <host> <port>: exit 0 if a TCP connection succeeds within 3s
  kc exec "pod/$web_pod" -c web -- python -c \
    "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('$1', $2)))" \
    2>/dev/null
}
probe iki-db-rw 5432 || fail "web pod cannot reach Postgres"
probe keycloak.iki.localhost 8080 || fail "web pod cannot reach Keycloak (the identity provider)"
if probe seaweedfs 8333; then fail "web pod reached SeaweedFS; default-deny egress is not enforced"; fi
if probe otel-lgtm 3000; then fail "web pod reached Grafana; only the OTLP port is allowed"; fi
pass "egress is default-deny: Postgres, Keycloak and OTLP allowed, everything else blocked"

# --- Keycloak -------------------------------------------------------------------
issuer=$(curl -fsS http://127.0.0.1:18080/realms/iki/.well-known/openid-configuration | jq -r .issuer)
[ "$issuer" = "http://keycloak.iki.localhost:8080/realms/iki" ] || fail "unexpected issuer: $issuer"
pass "Keycloak serves the iki realm (issuer $issuer)"

# --- Sign-in ----------------------------------------------------------------------
# The real wizard and Keycloak's real login page, driven like a browser.
uv run --quiet python "$(dirname "${BASH_SOURCE[0]}")/sso.py" --check \
  --iki-port 18000 --keycloak-port 18080 --web "pod/$web_pod"

# --- SeaweedFS (S3) ---------------------------------------------------------------
anon=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18333/)
[ "$anon" = 403 ] || fail "anonymous S3 request returned $anon"
buckets=$(curl -fsS --aws-sigv4 "aws:amz:us-east-1:s3" \
  --user "$(secret seaweedfs-s3 access-key):$(secret seaweedfs-s3 secret-key)" \
  http://127.0.0.1:18333/)
echo "$buckets" | grep -q "<Name>iki</Name>" || fail "bucket iki missing: $buckets"
pass "S3 rejects anonymous requests and lists bucket iki with signed ones"

echo "All end-to-end checks passed."
