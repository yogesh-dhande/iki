"""The database pool under the real server: whatever people do, connections go back, so
the pool never runs dry while the database has room."""

import http.client
import socket
import time
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from org_world import build_world
from real_server import app_role, serve, server_env

pytestmark = pytest.mark.django_db(transaction=True)

#: A search the API refuses (a condition with no value).
REFUSED = (
    '{"type": "acme/protein.Protein", "exact_type": false, "limit": 5, '
    '"where": {"field": "name", "op": "eq", "value": null}}'
)


def test_connections_go_back_whatever_clients_do(django_db_blocker: Any, tmp_path: Any) -> None:
    world = build_world()
    client = Client()
    client.force_login(world.users["viewer"])
    cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
    headers = {"Cookie": f"iki_session={cookie}"}
    pool = {"IKI_DB_POOL_MIN_SIZE": "1", "IKI_DB_POOL_MAX_SIZE": "2"}
    with django_db_blocker.unblock(), app_role() as role:
        env = server_env(
            role,
            tmp_path,
            IKI_SECRET_KEY=settings.SECRET_KEY,
            IKI_DB_POOL_TIMEOUT_SECONDS="3",
            **pool,
        )
        with serve(env) as server:
            host, port = server.url.removeprefix("http://").split(":")

            def ask(method: str, path: str, body: str | None = None) -> int:
                conn = http.client.HTTPConnection(host, int(port), timeout=10)
                extra = {"Content-Type": "application/json"} if body else {}
                conn.request(method, path, body=body, headers={**headers, **extra})
                status = conn.getresponse().status
                conn.close()
                return status

            def abandon(path: str) -> None:
                raw = socket.create_connection((host, int(port)))
                raw.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: iki_session={cookie}"
                    "\r\n\r\n".encode()
                )
                raw.close()

            def healthy(label: str) -> None:
                started = time.monotonic()
                status = ask("GET", "/api/v1/me")
                assert status == 200, f"after {label}: {status}\n{server.text[-3000:]}"
                assert time.monotonic() - started < 2, f"after {label}: slow\n{server.text[-3000:]}"

            for _ in range(10):
                assert ask("GET", "/api/v1/me") == 200
            healthy("plain requests")
            for _ in range(10):
                abandon("/api/v1/entity-types")
            time.sleep(1)
            healthy("abandoned requests")
            for _ in range(5):
                abandon("/api/v1/events")
            time.sleep(1)
            healthy("abandoned streams")
            for _ in range(5):
                ask("POST", "/api/v1/entities/search", REFUSED)
            healthy("refused searches")
            for _ in range(5):
                ask("GET", f"/projects/{world.project.slug}/")
                ask("GET", "/projects/")
            healthy("pages")


def test_a_connection_dropped_while_out_of_the_pool_goes_back() -> None:
    """asgiref ends each request's thread when it's done, and the thread's connection
    wrapper with it. One dropped while it holds a pooled connection mustn't take its
    place in the pool with it."""
    import gc

    from django.db import connection

    from iki_server.core.postgresql.base import DatabaseWrapper

    pool = {"min_size": 1, "max_size": 1, "timeout": 2}
    settings_dict = {
        **connection.settings_dict,
        "OPTIONS": {**connection.settings_dict["OPTIONS"], "pool": pool},
    }
    # The test suite's own connection has no pool, so this one has the alias to itself.
    alias = "default"
    # Django keeps each alias's pool here, made by the first connection.
    pools: dict[str, Any] = DatabaseWrapper._connection_pools  # type: ignore[attr-defined]
    assert alias not in pools
    try:
        # The pool keeps the wrapper that made it (Django configures connections with
        # it), so it's made first, as a server's first request does.
        first = DatabaseWrapper(settings_dict, alias)
        first.ensure_connection()
        first.close()

        dropped = DatabaseWrapper(settings_dict, alias)
        dropped.ensure_connection()
        del dropped
        gc.collect()

        again = DatabaseWrapper(settings_dict, alias)
        again.ensure_connection()
        with again.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
        again.close()
    finally:
        made = pools.pop(alias, None)
        if made is not None:
            made.close()
