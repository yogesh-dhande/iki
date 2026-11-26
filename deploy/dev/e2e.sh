#!/usr/bin/env bash
# End-to-end checks against the running dev cluster (after `tilt ci` or
# `tilt up`). Each check prints what it verified; any failure exits non-zero.
set -euo pipefail

CONTEXT=kind-iki
NS=iki
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
kc() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
pass() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
secret() { kc get secret "$1" -o "jsonpath={.data.$2}" | base64 -d; }

PIDS=()
cleanup() {
  if (( ${#PIDS[@]} )); then
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  fi
}
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

locks=("$here/plugins.lock.yaml")
sso_args=(--check)
case ${IKI_DEV_FIXTURES:-0} in
  0) profile=product ;;
  1)
    profile=fixtures
    locks+=("$here/fixtures/plugins.lock.yaml")
    sso_args+=(--fixtures --lims-port 18091 --foldcloud-port 18092)
    ;;
  *) fail "IKI_DEV_FIXTURES must be 0 or 1" ;;
esac
echo "Iki dev cluster end-to-end checks ($profile)"

# Wait for every rollout in progress, so logs, execs and port-forwards hit the current
# pods: a dependency's new image (SeaweedFS's, say) replaces its pod too, and a forward to
# its Service can land on the old one as it shuts down.
for deployment in $(kc get deploy -o name); do
  kc rollout status "$deployment" --timeout=180s >/dev/null || fail "$deployment did not roll out"
done

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
# The address Iki signs upload URLs for (Tilt's port-forward when it's up): sso.py's
# checks and the files checks below send parts there, as a browser does.
if ! nc -z 127.0.0.1 8333 2>/dev/null; then forward svc/seaweedfs 8333 8333; fi
forward svc/otel-lgtm 13200 3200
forward svc/mailpit 18025 8025
forward svc/webhook-receiver 18090 8080
if [[ "$profile" == fixtures ]]; then
  forward svc/fake-lims 18091 8080
  forward svc/fake-foldcloud 18092 8080
else
  services=$(kc get service fake-lims fake-foldcloud --ignore-not-found -o name)
  [[ -z "$services" ]] || fail "product cluster runs fixture services: $services"
fi

# Check the running images against the selected source pins before the seed makes records.
expected=$(uv run --quiet python - "${locks[@]}" <<'PYTHON'
import sys
from pathlib import Path

import yaml

names = [
    pin["name"]
    for path in sys.argv[1:]
    for pin in yaml.safe_load(Path(path).read_text())["plugins"]
    if pin["state"] != "purged"
]
print("\n".join(sorted(names)))
PYTHON
)
check_image_plugins() {
  local names
  names=$(kc exec "$1" -c "$2" -- cat "$3" |
    jq -r '[.plugins[] | select(.state != "purged") | .name] | sort[]')
  [[ "$names" == "$expected" ]] || fail "$2 plugin names differ from the $profile lock: $names"
}
check_image_plugins "pod/$web_pod" web /opt/iki/plugins/iki_installed_plugins/lock.json
check_image_plugins deploy/iki-worker worker /opt/iki/plugins/plugins.json
pass "the running server and worker include the plugins pinned for $profile"

# --- Iki web ------------------------------------------------------------------
ready=$(curl -fsS http://127.0.0.1:18000/healthz/ready)
[ "$ready" = '{"status": "ok", "checks": {"database": "ok", "migrations": "ok", "database_role": "ok"}}' ] \
  || fail "readiness: $ready"
pass "web is ready: Postgres reachable over verified TLS, migrations applied, least-privileged role"

# The migrate Job ran the plugins' migrations, their guards' sync and the app role's grants
# as one transaction (ADR 0134), so old pods never saw a table without its guards.
migrated=$(kc logs job/iki-migrate -c migrate 2>&1) || fail "no log from the migrate Job: $migrated"
grep -q "committed in one transaction" <<<"$migrated" \
  || fail "the migrate Job didn't commit in one transaction: $(tail -5 <<<"$migrated")"
grep -q "^Granted app privileges to " <<<"$migrated" || fail "the migrate Job granted nothing"
pass "the migrate Job migrated, synced the plugins' guards and granted the app role in one transaction"

# The app's role may write rows, but not change the schema or the audit log.
denied=$(kc exec "pod/$web_pod" -c web -- iki-server shell -c '
from django.db import connection
for statement in (
    "UPDATE iki_audit_event SET action = action",
    "DELETE FROM iki_audit_event",
    "CREATE TABLE e2e_sneaky (id int)",
    "INSERT INTO django_migrations (app, name, applied) VALUES (1, 1, now())",
):
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
        print("ALLOWED", statement)
    except Exception as error:
        print("denied" if "permission denied" in str(error) else f"ERROR {error}")
' 2>&1)
[ "$(grep -c '^denied$' <<<"$denied")" = 4 ] || fail "app role has too much power: $denied"
pass "the app's database role can't change the schema, migration history or audit log"

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
probe mailpit 1025 || fail "web pod cannot reach the mail server (Mailpit)"
probe seaweedfs 8333 || fail "web pod cannot reach object storage (SeaweedFS)"
if probe otel-lgtm 3000; then fail "web pod reached Grafana; only the OTLP port is allowed"; fi
pass "egress is default-deny: Postgres, Keycloak, OTLP, SMTP and S3 allowed, everything else blocked"
tasks_probe() { # as probe, from a task runner
  kc exec deploy/iki-tasks -c tasks -- python -c \
    "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('$1', $2)))" \
    2>/dev/null
}
tasks_probe webhook-receiver 8080 || fail "task runners cannot reach the webhook receiver"
if probe webhook-receiver 8080; then fail "web pod reached the webhook receiver; only task runners send"; fi
pass "only task runners reach webhook receivers"
tasks_probe seaweedfs 8333 || fail "task runners cannot reach object storage (SeaweedFS)"
pass "task runners reach object storage"
worker_python() { # runs Python in a worker pod, as a run's command would
  kc exec deploy/iki-worker -c worker -- /opt/iki/envs/iki__dna/sequence-tools/bin/python -I -c "$1" 2>/dev/null
}
if worker_python "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('1.1.1.1', 443)))"; then
  fail "a worker pod reached the internet directly; runs go through the egress proxy"
fi
refused=$(worker_python "
import socket
s = socket.create_connection(('iki-egress-proxy', 3128), timeout=5)
s.sendall(b'CONNECT eutils.ncbi.nlm.nih.gov:443 HTTP/1.1\r\n\r\n')
print(s.recv(200).split(b'\r\n')[0].decode())") || fail "a worker pod cannot reach the egress proxy"
[ "$refused" = "HTTP/1.1 407 Proxy Authentication Required" ] ||
  fail "the egress proxy answered a CONNECT without credentials with: $refused"
if probe iki-egress-proxy 3128; then fail "web pod reached the egress proxy; only workers do"; fi
pass "workers reach the internet only through the egress proxy, which wants a run's credentials"
worker_probe() { # as probe, from a worker pod, as a run's command would
  worker_python "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('$1', $2)))"
}
worker_probe iki-web 8000 || fail "a worker pod cannot reach the server (the web pods)"
worker_probe seaweedfs 8333 || fail "a worker pod cannot reach object storage (SeaweedFS)"
if worker_probe keycloak.iki.localhost 8080; then fail "a worker pod reached Keycloak; workers reach only the server, storage and the proxy"; fi
if worker_probe iki-db-rw 5432; then fail "a worker pod reached Postgres; workers never reach the database"; fi
pass "workers reach the server and object storage, and not the identity provider or the database"

# --- Export sandbox (ADR 0230) ------------------------------------------------------
# The fixture build has a plugin writer (examples/liquid_handling's Tecan worklist), so
# it runs the sandbox. Its pod must hold nothing and reach nothing; sso.py exports a
# file through it.
if [[ "$profile" == fixtures ]]; then
  forward deploy/iki-sandbox 18191 8091
  sandbox_ready=$(curl -sS -w ' HTTP %{http_code}' http://127.0.0.1:18191/healthz/ready)
  [[ "$sandbox_ready" == *" HTTP 200" ]] || fail "the sandbox isn't ready: $sandbox_ready"
  pass "the sandbox is ready: every writer module in the image loaded and passed its checks"

  # Addresses read from outside, so the sandbox's missing DNS can't make a probe pass.
  cluster_ip() { kubectl --context "$CONTEXT" -n "$1" get service "$2" -o 'jsonpath={.spec.clusterIP}'; }
  pod_ip() { kc get pods -l "$1" -o 'jsonpath={.items[0].status.podIP}'; }
  api_endpoint=$(kubectl --context "$CONTEXT" -n default get endpointslices \
    -l kubernetes.io/service-name=kubernetes -o 'jsonpath={.items[0].endpoints[0].addresses[0]}:{.items[0].ports[0].port}')
  db_ip=$(cluster_ip "$NS" iki-db-rw)
  targets=(
    "$db_ip:5432"
    "$(pod_ip cnpg.io/cluster=iki-db):5432"
    "$(cluster_ip "$NS" iki-web):8000"
    "$(pod_ip app.kubernetes.io/component=web):8000"
    "$(cluster_ip "$NS" seaweedfs):8333"
    "$(cluster_ip "$NS" iki-egress-proxy):3128"
    "$(cluster_ip kube-system kube-dns):53"
    "$(cluster_ip default kubernetes):443"
    "$api_endpoint"
    "169.254.169.254:80"
    "1.1.1.1:443"
  )
  for target in "${targets[@]}"; do
    [[ "$target" =~ ^[0-9a-f.:]+:[0-9]+$ ]] || fail "couldn't read an address to probe from the sandbox: '$target'"
  done
  # The same probe reaches Postgres from a web pod, so a refusal below is the policy's.
  probe "$db_ip" 5432 || fail "web pod cannot reach Postgres at $db_ip:5432 (the probe's control)"
  isolation=$(kc exec deploy/iki-sandbox -c sandbox -- /opt/iki/venv/bin/python -I -c '
import os
import socket
import sys
import threading

problems = []
if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount"):
    problems.append("a service account token is mounted")
# The server image sets IKI_STATIC_ROOT (a path) for every container; nothing else.
IMAGE = {"IKI_STATIC_ROOT"}


def unwanted(names):
    return sorted(
        n for n in names
        if (n.startswith(("IKI_", "AWS_", "GOOGLE_")) or "DATABASE" in n) and n not in IMAGE
    )


with open("/proc/1/environ", "rb") as f:
    launcher = [e.split(b"=", 1)[0].decode() for e in f.read().split(b"\0") if e]
if unwanted(os.environ):
    problems.append(f"the container has {unwanted(os.environ)}")
# The launcher re-executes itself with nothing in its environment at all.
if launcher:
    problems.append(f"the sandbox process has {sorted(launcher)}")
found = {}


def look_up():
    try:
        found["address"] = [a[4][0] for a in socket.getaddrinfo("kubernetes.default.svc", 443)]
    except OSError as error:
        found["error"] = error


lookup = threading.Thread(target=look_up, daemon=True)
lookup.start()
lookup.join(30)
for address in found.get("address", ()):
    problems.append(f"DNS resolved kubernetes.default.svc to {address}")
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.settimeout(3)
    try:
        if s.connect_ex((host, int(port))) == 0:
            problems.append(f"connected to {target}")
    except OSError:
        pass
    finally:
        s.close()
print("\n".join(problems) or "isolated")
sys.exit(1 if problems else 0)
' "${targets[@]}" 2>&1) || fail "the sandbox isn't isolated: $isolation"
  [[ "$isolation" == isolated ]] || fail "the sandbox's isolation check said: $isolation"
  pass "the sandbox has no service account token and no IKI_, DATABASE, AWS_ or GOOGLE_ variables"
  pass "the sandbox resolves no names and reaches nothing: Postgres, web, S3, the egress proxy, DNS, the Kubernetes API (${targets[7]}, $api_endpoint), the metadata service, the internet"

  sandbox_ip=$(pod_ip app.kubernetes.io/component=sandbox)
  probe iki-sandbox 8090 || fail "web pod cannot reach the sandbox"
  tasks_probe iki-sandbox 8090 || fail "task runners cannot reach the sandbox"
  if probe "$sandbox_ip" 8091; then fail "web pod reached the sandbox's health port; only exports are open"; fi
  if worker_probe iki-sandbox 8090; then fail "a worker pod reached the sandbox; only web pods and task runners do"; fi
  if worker_probe "$sandbox_ip" 8090; then fail "a worker pod reached the sandbox's pod directly"; fi
  pass "web pods and task runners reach the sandbox's export port; workers don't, nor anyone its health port"
else
  sandbox=$(kc get deploy iki-sandbox --ignore-not-found -o name)
  [[ -z "$sandbox" ]] || fail "product cluster runs the export sandbox: $sandbox"
fi

# --- Keycloak -------------------------------------------------------------------
issuer=$(curl -fsS http://127.0.0.1:18080/realms/iki/.well-known/openid-configuration | jq -r .issuer)
[ "$issuer" = "http://keycloak.iki.localhost:8080/realms/iki" ] || fail "unexpected issuer: $issuer"
pass "Keycloak serves the iki realm (issuer $issuer)"

# --- Sign-in ----------------------------------------------------------------------
# The real wizard and Keycloak's real login page, driven like a browser.
uv run --directory "$here/../.." --quiet python -m deploy.dev.sso "${sso_args[@]}" \
  --iki-port 18000 --keycloak-port 18080 --mailpit-port 18025 --webhook-port 18090 --web "pod/$web_pod"

# --- SeaweedFS (S3) ---------------------------------------------------------------
anon=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18333/)
[ "$anon" = 403 ] || fail "anonymous S3 request returned $anon"
buckets=$(curl -fsS --aws-sigv4 "aws:amz:us-east-1:s3" \
  --user "$(secret seaweedfs-s3 access-key):$(secret seaweedfs-s3 secret-key)" \
  http://127.0.0.1:18333/)
echo "$buckets" | grep -q "<Name>iki</Name>" || fail "bucket iki missing: $buckets"
pass "S3 rejects anonymous requests and lists bucket iki with signed ones"

# --- Files -----------------------------------------------------------------------
# A part URL signed by the web pod, for the address browsers use
# (localhost:8333, Tilt's port-forward), takes bytes from this machine; the
# pod then assembles the part and reads it back. Nothing is left behind.
e2e_key="e2e/$(date +%s)-$RANDOM"
signed=$(kc exec "pod/$web_pod" -c web -- python -m iki_server.manage shell -c "
from iki_server.files import storage
upload = storage.start_multipart('$e2e_key')
print(upload, storage.part_url('$e2e_key', upload, 1))" | tail -1)
multipart=${signed%% *}
url=${signed#* }
case "$url" in http://localhost:8333/*) ;; *) fail "part URL doesn't name the public endpoint: $url" ;; esac
payload="iki end-to-end $e2e_key"
curl -fsS -X PUT --data-binary "$payload" "$url" >/dev/null || fail "PUT to the signed part URL failed"
expected=$(printf '%s' "$payload" | shasum -a 256 | cut -d' ' -f1)
got=$(kc exec "pod/$web_pod" -c web -- python -m iki_server.manage shell -c "
from iki_server.files import storage
parts = storage.uploaded_parts('$e2e_key', '$multipart')
storage.complete_multipart('$e2e_key', '$multipart', {1: parts[1][0][1]})
print(storage.digest('$e2e_key')[0])
storage.delete('$e2e_key')" | tail -1)
[ "$got" = "$expected" ] || fail "the stored bytes hash to $got, not $expected"
pass "a part sent to a URL the web pod signed is stored intact (SHA-256 $expected)"

# Registering in place: the web pod pins a version of an object in the lab
# bucket, which a later write doesn't change, and signs a download of it.
lab_key="runs/e2e-$(date +%s)-$RANDOM.csv"
pinned=$(kc exec "pod/$web_pod" -c web -- python -m iki_server.manage shell -c "
from iki_server.files import storage
s3 = storage.client_for('lab-data')
first = s3.put_object(Bucket='lab-data', Key='$lab_key', Body=b'well,od600\\nA1,0.42\\n')['VersionId']
s3.put_object(Bucket='lab-data', Key='$lab_key', Body=b'overwritten')
assert storage.versioned('lab-data')
assert storage.head('lab-data', '$lab_key', first)['version'] == first
sha, size = storage.digest_object('lab-data', '$lab_key', first)
print(sha, storage.download_url('s3://lab-data/$lab_key', first, 'plate.csv', 'text/csv'))" | tail -1)
lab_sha=${pinned%% *}
lab_url=${pinned#* }
fetched=$(curl -fsS "$lab_url" | shasum -a 256 | cut -d' ' -f1)
want=$(printf 'well,od600\nA1,0.42\n' | shasum -a 256 | cut -d' ' -f1)
[ "$lab_sha" = "$want" ] && [ "$fetched" = "$want" ] ||
  fail "the pinned version reads as $lab_sha and downloads as $fetched, not $want"
kc exec "pod/$web_pod" -c web -- python -m iki_server.manage shell -c "
from iki_server.files import storage
s3 = storage.client_for('lab-data')
for v in s3.list_object_versions(Bucket='lab-data', Prefix='$lab_key').get('Versions', []):
    s3.delete_object(Bucket='lab-data', Key='$lab_key', VersionId=v['VersionId'])" >/dev/null
pass "a registered object's version is pinned: a later write doesn't change what downloads"

echo "All end-to-end checks passed."
