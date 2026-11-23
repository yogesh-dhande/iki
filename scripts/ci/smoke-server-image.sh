#!/usr/bin/env bash
# Smoke checks for the final server image on either native release platform.
set -euo pipefail
image=${1:?Pass the local server image tag.}
platform=${2:?Pass its expected Linux platform.}
artifacts=$(cd "${3:?Pass the checked Wasm artifact directory.}" && pwd)

case "$platform" in
  linux/amd64) machine=x86_64 ;;
  linux/arm64) machine=aarch64 ;;
  *) echo "Unsupported server image platform: $platform" >&2; exit 1 ;;
esac
host=$(uname -m)
if [[ "$host" == arm64 ]]; then host=aarch64; fi
if [[ "$host" != "$machine" ]]; then
  echo "The $platform smoke test requires a native $machine host, found $host." >&2
  exit 1
fi
actual=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")
if [[ "$actual" != "$platform" ]]; then
  echo "Expected $platform for $image, found $actual." >&2
  exit 1
fi

run=(docker run --pull=never --platform "$platform" --network none)
environment=(
  -e IKI_SECRET_KEY="$(openssl rand -base64 48)"
  -e IKI_ALLOWED_HOSTS=iki.example.com
  -e IKI_ENVIRONMENT=ci
  -e IKI_DATABASE_URL=postgres://iki:unused@db.invalid/iki
  -e IKI_ENCRYPTION_KEYS="$(openssl rand -base64 32 | tr '+/' '-_')"
)
"${run[@]}" --rm "${environment[@]}" "$image" check --deploy --fail-level WARNING

# Verify the installed payload and native dependency using the shipped interpreter.
"${run[@]}" --rm -i --entrypoint python \
  --mount "type=bind,source=$artifacts,target=/server-wasm,readonly" "$image" - "$machine" <<'PY'
import gzip
import json
import os
import platform
import sys
from importlib.resources import files
from pathlib import Path

import psycopg

machine = platform.machine()
assert machine == sys.argv[1], f"expected {sys.argv[1]}, runtime reports {machine}"
assert os.getuid() == 10001, f"unexpected runtime UID {os.getuid()}"
assert psycopg.pq.__impl__ == "c", psycopg.pq.__impl__
print(f"Runtime architecture: {machine}, UID {os.getuid()}, psycopg C")

artifact = Path("/server-wasm")
installed = files("iki_spec")
for name in (
    "iki_wasm.wasm",
    "dna-annotation.wasm",
    "oligo-measurement.wasm",
    "rust-standard-library.html",
):
    assert installed.joinpath(name).read_bytes() == (artifact / name).read_bytes(), name

static = Path("/opt/iki/static")
assert json.loads((static / "staticfiles.json").read_bytes())["paths"]
app = static / "app"
manifest = json.loads((app / "manifest.json").read_bytes())
main = app / manifest["src/main.tsx"]["file"]
browser_modules = list((app / "assets").glob("iki_wasm-*.wasm"))
assert len(browser_modules) == 1, browser_modules
assert browser_modules[0].read_bytes() == (artifact / "iki_wasm.wasm").read_bytes()
styles = list((static / "iki").glob("iki.*.css.gz"))
assert styles, "missing collected stylesheets"
for path in [*styles, Path(f"{main}.gz")]:
    with gzip.open(path, "rb") as compressed:
        assert compressed.read(), path
print("Verified installed and browser Wasm and collected static files")
PY

# Start the image's default server command and probe it from inside the container.
# Liveness does not require a database or a published port.
container=
cleanup() {
  if [[ -n "$container" ]]; then
    if [[ "$1" != 0 ]]; then docker logs "$container" >&2 || true; fi
    docker rm -f "$container" >/dev/null || true
  fi
}
trap 'cleanup "$?"' EXIT
container=$("${run[@]}" --detach "${environment[@]}" "$image")
docker exec -i "$container" python - <<'PY'
import json
import time
import urllib.error
import urllib.request

deadline = time.monotonic() + 30
while True:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz/live", timeout=2) as response:
            health = json.load(response)
        break
    except urllib.error.URLError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.25)
assert health == {"status": "ok", "checks": {}}, health
print("Server reports healthy")
PY
