"""The real server in a subprocess: production settings, collected static files,
uvicorn, and the least-privileged database role, as a deployment runs it."""

import contextlib
import http.client
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
from urllib.parse import urlsplit

from django.conf import settings

#: How long a process has to answer its health check. Starting is importing Django and
#: every plugin: seconds alone, and more than 30 while other checks load the machine.
STARTUP = 90.0

#: What asking a process that's starting, or busy, can raise: not listening yet (refused),
#: an error status (``HTTPError``, a ``URLError``), no answer within the timeout (a bare
#: ``TimeoutError``, which isn't a ``URLError``), or the connection dropped mid-answer.
NOT_ANSWERING = (OSError, http.client.HTTPException)

#: The database roles of the servers and runners running now, whose requests the test
#: database waits for before it's emptied (see `quiet`).
_ROLES: list[str] = []


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
def serve(env: dict[str, str], *, startup: float = STARTUP) -> Iterator[Server]:
    """Runs ``iki-server serve`` until the block ends, then stops it with SIGTERM
    and waits for it to exit. It has ``startup`` seconds to answer its health check."""
    url = f"http://127.0.0.1:{env['IKI_PORT']}"
    with _running(["serve"], env, f"{url}/healthz/live", url, startup) as server:
        yield server


class _PortTakenError(RuntimeError):
    """The free port it was given was taken before it could listen (tests run in parallel)."""


@contextmanager
def run_tasks(env: dict[str, str]) -> Iterator[Server]:
    """Runs ``iki-server tasks`` beside a server, until the block ends. Another port is
    tried when the one it was given is taken first."""
    with contextlib.ExitStack() as stack:
        for attempt in range(1, 4):
            port = str(free_port())
            try:
                runner = stack.enter_context(
                    _running(
                        ["tasks", "--health-port", port, "--grace", "5"],
                        env,
                        f"http://127.0.0.1:{port}/healthz/live",
                        f"http://127.0.0.1:{port}",
                    )
                )
                break
            except _PortTakenError:
                if attempt == 3:
                    raise
        yield runner


def answer(url: str, *, within: float = STARTUP, timeout: float = 5) -> tuple[int, bytes]:
    """Asks ``url`` again until it answers, as a probe does: its status and body. A
    process's first answers can take seconds (a readiness check builds the migration
    graph once) while other checks load the machine. Raises the last error past
    ``within`` seconds."""
    deadline = time.monotonic() + within
    while True:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - our own
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()
        except NOT_ANSWERING:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def quiet(timeout: float = 30) -> None:
    """Waits for the real servers and runners to finish what they're doing in the test
    database. Closing a page, or killing a worker, doesn't stop its requests on the
    server, and a request still in a transaction when the database is emptied after a
    test deadlocks with the emptying (see conftest's ``_empties_past_late_requests``)."""
    from django.db import connection

    if not _ROLES:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND usename = ANY(%s) AND state <> 'idle'",
                [_ROLES],
            )
            (busy,) = cursor.fetchone()
        if not busy:
            return
        time.sleep(0.05)


@contextmanager
def _running(
    args: list[str], env: dict[str, str], health: str, url: str, startup: float = STARTUP
) -> Iterator[Server]:
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
        deadline = time.monotonic() + startup
        while True:
            try:
                with urllib.request.urlopen(health, timeout=2):  # noqa: S310 - our own
                    break
            except NOT_ANSWERING:
                if time.monotonic() > deadline or proc.poll() is not None:
                    if proc.poll() is not None:
                        reader.join(timeout=5)
                    error = (
                        _PortTakenError if "Address already in use" in server.text else RuntimeError
                    )
                    raise error(f"{args[0]} did not start\n{server.text}") from None
                time.sleep(0.2)
        role = urlsplit(env["IKI_DATABASE_URL"]).username or ""
        _ROLES.append(role)
        try:
            yield server
        finally:
            _ROLES.remove(role)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        reader.join(timeout=5)
        stdout.close()
