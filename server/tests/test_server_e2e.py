"""Boots the real ASGI server in a subprocess and checks it end to end:
probes over HTTP, JSON logs on stdout, and traces exported over OTLP.
"""

import http.server
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db(transaction=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class FakeCollector:
    """A minimal OTLP/HTTP endpoint that records which signal paths were posted to."""

    port: int
    paths: list[str] = field(default_factory=list)


@pytest.fixture
def collector() -> Iterator[FakeCollector]:
    fake = FakeCollector(port=free_port())

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            fake.paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", fake.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()
    thread.join()


def get(url: str, host: str | None = None) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(url, headers={"Host": host} if host else {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # noqa: S310 - local test URL
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        with err:
            return err.code, {}


def test_server_serves_probes_logs_json_and_exports_traces(
    collector: FakeCollector, tmp_path: Path
) -> None:
    db = settings.DATABASES["default"]
    port = free_port()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("IKI_", "DJANGO_", "OTEL_"))},
        "DJANGO_SETTINGS_MODULE": "iki_server.settings",
        "IKI_SECRET_KEY": "e2e-" + "s" * 60,
        "IKI_ALLOWED_HOSTS": "127.0.0.1",
        "IKI_ENVIRONMENT": "e2e",
        "IKI_ENCRYPTION_KEYS": settings.IKI_ENCRYPTION_KEYS[0],
        "IKI_DATABASE_URL": (
            f"postgres://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}"
        ),
        "IKI_DB_SSLMODE": "disable",
        "IKI_HTTPS": "false",
        "IKI_PORT": str(port),
        "IKI_HOST": "127.0.0.1",
        "IKI_STATIC_ROOT": str(tmp_path / "static"),
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{collector.port}",
        "OTEL_BSP_SCHEDULE_DELAY": "100",
        "OTEL_METRIC_EXPORT_INTERVAL": "100000",
    }
    # Static files are collected at image build time; do the same here.
    collected = subprocess.run(
        [sys.executable, "-m", "iki_server.manage", "collectstatic", "--noinput"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    proc = subprocess.Popen(
        [sys.executable, "-m", "iki_server.manage", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                status, body = get(f"http://127.0.0.1:{port}/healthz/live")
                break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline or proc.poll() is not None:
                    pytest.fail("server did not start")
                time.sleep(0.2)
        assert status == 200

        status, body = get(f"http://127.0.0.1:{port}/healthz/ready")
        assert (status, body) == (
            200,
            {"status": "ok", "checks": {"database": "ok", "migrations": "ok"}},
        )

        # Kubelet probes send the pod IP and port as Host, which isn't in ALLOWED_HOSTS.
        for path in ("/healthz/live", "/healthz/ready"):
            status, _ = get(f"http://127.0.0.1:{port}{path}", host="10.244.0.13:8000")
            assert status == 200, path

        status, _ = get(f"http://127.0.0.1:{port}/missing?token=secret")
        assert status == 404

        # Pages render and link to content-hashed static files with long-lived caching.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/setup/", timeout=5) as resp:
            page = resp.read().decode()
            assert "Content-Security-Policy" in resp.headers
        css = re.search(r'href="(/static/iki/iki\.[0-9a-f]{12}\.css)"', page)
        assert css, page
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{css.group(1)}", timeout=5) as resp:
            assert resp.status == 200
            assert "immutable" in resp.headers["Cache-Control"]

        deadline = time.monotonic() + 10
        while "/v1/traces" not in collector.paths and time.monotonic() < deadline:
            time.sleep(0.1)
        assert "/v1/traces" in collector.paths
    finally:
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=30)

    # uvicorn shuts down gracefully, then re-raises SIGTERM so the exit status reflects it.
    assert proc.returncode in (0, -signal.SIGTERM), output
    assert "Finished server process" in output
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert lines, output
    assert all(line.startswith("{") for line in output.splitlines() if line.strip()), output
    request = next(line for line in lines if line["event"] == "request")
    assert request["path"] == "/missing"
    assert request["status"] == 404
    assert len(str(request["trace_id"])) == 32
    assert "secret" not in output
    assert "color_message" not in output
    assert any(line["logger"].startswith("uvicorn") for line in lines)
