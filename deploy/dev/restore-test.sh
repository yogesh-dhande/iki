#!/usr/bin/env bash
# The automated restore test (ADR 0099), on the dev cluster: back the database up, restore it
# into a new database, and check the restore with `iki-server check_restore`, against the
# same files bucket and keys. It must hold everything the live database held when the
# backup began, and every file its records name must be in storage. It drops the restored
# database when it's done. Run after `tilt ci` and the e2e checks, so there's data.
set -euo pipefail

CONTEXT=kind-iki
NS=iki
DB_POD=iki-db-1
RESTORED=iki_restore_test
kc() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
pass() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
psql_() { kc exec "$DB_POD" -c postgres -- psql -v ON_ERROR_STOP=1 -qtAX "$@"; }

work=$(mktemp -d)
drop() { psql_ -c "DROP DATABASE IF EXISTS $RESTORED WITH (FORCE)" >/dev/null 2>&1 || true; }
cleanup() { drop; rm -r -f -- "$work"; }
trap cleanup EXIT

# check_restore in a web pod, against the database named $1.
check() {
  local db=$1
  shift
  # shellcheck disable=SC2016 # expanded by the pod's shell, from its own environment
  kc exec deploy/iki-web -c web -- sh -c \
    'IKI_DATABASE_URL="${IKI_DATABASE_URL%/*}/$0" IKI_DB_POOL=false exec iki-server check_restore --json "$@"' \
    "$db" "$@"
}

echo "Iki restore test"
kc rollout status deploy/iki-web --timeout=180s >/dev/null

# What the live deployment holds, just before the backup begins.
check iki --sample 50 >"$work/live.json" || fail "the live deployment doesn't pass check_restore: $(cat "$work/live.json")"
pass "the live deployment passes check_restore"

started=$(date +%s)
kc exec "$DB_POD" -c postgres -- pg_dump --format=custom --dbname=iki >"$work/iki.dump"
size=$(wc -c <"$work/iki.dump" | tr -d ' ')
[ "$size" -gt 0 ] || fail "pg_dump wrote nothing"
pass "backed the database up ($size bytes)"

drop
psql_ -c "CREATE DATABASE $RESTORED OWNER iki"
kc exec -i "$DB_POD" -c postgres -- pg_restore --exit-on-error --dbname="$RESTORED" <"$work/iki.dump"
pass "restored it into $RESTORED in $(($(date +%s) - started)) seconds"

# Every file's size, and a sample's bytes against their SHA-256.
check "$RESTORED" >"$work/restored.json" || fail "the restore doesn't pass check_restore: $(cat "$work/restored.json")"
check "$RESTORED" --hashes --sample 20 >"$work/hashed.json" || fail "the restore's files don't match their SHA-256: $(cat "$work/hashed.json")"
pass "the restore passes check_restore, files' sizes and a sample's SHA-256 too"

python3 - "$work/live.json" "$work/restored.json" <<'EOF'
import json
import sys

def report(path: str) -> dict:
    """The report's line, among the log lines Iki writes to stdout too."""
    for line in open(path):
        if line.startswith("{"):
            found = json.loads(line)
            if "checks" in found:
                return found
    sys.exit(f"no report in {path}")


live, restored = report(sys.argv[1]), report(sys.argv[2])
lost = {
    name: (n, restored["counts"].get(name, 0))
    for name, n in live["counts"].items()
    if restored["counts"].get(name, 0) < n
}
if lost:
    sys.exit(f"the restore holds less than the live deployment did: {lost}")
if (live["data_until"] or "") > (restored["data_until"] or ""):
    sys.exit(f"the restore's data ends at {restored['data_until']}, before {live['data_until']}")
files = {c["name"]: c["checked"] for c in restored["checks"]}["Files in storage"]
if live["counts"]["file_contents"] and not files:
    sys.exit("no file was checked")
print(f"  holds {restored['counts']}; data until {restored['data_until']}; {files} files checked")
EOF
pass "the restore holds everything the live deployment did when the backup began"
