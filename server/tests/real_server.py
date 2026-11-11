"""The real server in a subprocess: production settings, collected static files,
uvicorn, and the least-privileged database role, as a deployment runs it."""

import io
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def app_role() -> Iterator[tuple[str, str]]:
    """A least-privileged app role, granted exactly as the migrate Job does in production."""
    from django.core.management import call_command
    from django.db import connection

    name, password = f"iki_e2e_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {name} LOGIN PASSWORD %s", [password])
    call_command("grant_app_privileges", "--role", name, stdout=io.StringIO())
    try:
        yield name, password
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP OWNED BY {name}")
            cursor.execute(f"DROP ROLE {name}")


def server_env(role: tuple[str, str], static_root: Path, **extra: str) -> dict[str, str]:
    db = settings.DATABASES["default"]
    user, password = role
    inherited = {
        k: v for k, v in os.environ.items() if not k.startswith(("IKI_", "DJANGO_", "OTEL_"))
    }
    return {
        **inherited,
        "DJANGO_SETTINGS_MODULE": "iki_server.settings",
        "IKI_SECRET_KEY": "e2e-" + "s" * 60,
        "IKI_ALLOWED_HOSTS": "127.0.0.1",
        "IKI_ENVIRONMENT": "e2e",
        "IKI_ENCRYPTION_KEYS": settings.IKI_ENCRYPTION_KEYS[0],
        "IKI_DATABASE_URL": f"postgres://{user}:{password}@{db['HOST']}:{db['PORT']}/{db['NAME']}",
        "IKI_DB_SSLMODE": "disable",
        "IKI_HTTPS": "false",
        "IKI_HOST": "127.0.0.1",
        "IKI_PORT": str(free_port()),
        "IKI_STATIC_ROOT": str(static_root),
        "OTEL_SDK_DISABLED": "true",
        **extra,
    }


def manage(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "iki_server.manage", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@dataclass
class Server:
    url: str
    process: "subprocess.Popen[str]"
    #: The environment it runs with.
    env: dict[str, str] = field(default_factory=dict)
    #: Everything the server printed, read as it goes so the pipe never fills.
    output: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.output)


@contextmanager
def serve(env: dict[str, str]) -> Iterator[Server]:
    """Runs ``iki-server serve`` until the block ends, then stops it with SIGTERM
    and waits for it to exit."""
    url = f"http://127.0.0.1:{env['IKI_PORT']}"
    with _running(["serve"], env, f"{url}/healthz/live", url) as server:
        yield server


@contextmanager
def run_tasks(env: dict[str, str]) -> Iterator[Server]:
    """Runs ``iki-server tasks`` beside a server, until the block ends."""
    port = str(free_port())
    with _running(
        ["tasks", "--health-port", port, "--grace", "5"],
        env,
        f"http://127.0.0.1:{port}/healthz/live",
        f"http://127.0.0.1:{port}",
    ) as runner:
        yield runner


@contextmanager
def _running(args: list[str], env: dict[str, str], health: str, url: str) -> Iterator[Server]:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "iki_server.manage", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    server = Server(url, proc, env)
    stdout = proc.stdout
    assert stdout is not None
    reader = threading.Thread(target=lambda: server.output.extend(stdout), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(health, timeout=2):  # noqa: S310 - our own
                    break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError(f"{args[0]} did not start\n{server.text}") from None
                time.sleep(0.2)
        yield server
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        reader.join(timeout=5)
        stdout.close()
