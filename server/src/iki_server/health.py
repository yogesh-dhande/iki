"""Liveness and readiness checks for Kubernetes probes.

- ``GET /healthz/live``: the process is up and serving. No dependencies are
  checked, so a database outage never makes Kubernetes restart healthy pods.
- ``GET /healthz/ready``: the pod can serve traffic. Postgres must answer,
  every migration this code knows about must be applied, and the database
  role must have no more power than the app needs.

Both run before host validation and HTTPS redirects (see ``MIDDLEWARE``),
because kubelet probes connect over plain HTTP using the pod IP. Responses
name each check and its result, but never error details. Failures are
logged on the server instead.
"""

from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, HttpResponseBase, JsonResponse

from iki_server.core import db_roles

logger = structlog.get_logger("iki.health")

LIVE_PATH = "/healthz/live"
READY_PATH = "/healthz/ready"

CheckResult = Literal["ok", "fail"]
GetResponse = Callable[[HttpRequest], Awaitable[HttpResponseBase]]


def check_database() -> CheckResult:
    try:
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("readiness check failed", check="database")
        return "fail"
    return "ok"


class _MigrationState:
    """Remembers once all migrations are applied.

    A running process never un-applies migrations, so after the first
    success the (comparatively expensive) migration graph isn't rebuilt.
    """

    applied = False


def check_migrations() -> CheckResult:
    if _MigrationState.applied:
        return "ok"
    try:
        connection = connections[DEFAULT_DB_ALIAS]
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:
        logger.exception("readiness check failed", check="migrations")
        return "fail"
    if pending:
        logger.warning("readiness check failed", check="migrations", pending=len(pending))
        return "fail"
    _MigrationState.applied = True
    return "ok"


class _RoleState:
    """Remembers once the database role is known to be least-privileged."""

    checked = False


def check_database_role() -> CheckResult:
    """Fails if the app connects with more power than it needs (ADR 0025).

    A pod given the migrator's credentials, or a superuser's, never turns
    ready, so the mistake is caught at deploy time.
    """
    if not settings.IKI_DB_REQUIRE_LEAST_PRIVILEGE or _RoleState.checked:
        return "ok"
    try:
        found = db_roles.problems()
    except Exception:
        logger.exception("readiness check failed", check="database_role")
        return "fail"
    if found:
        logger.error("readiness check failed", check="database_role", problems=found)
        return "fail"
    _RoleState.checked = True
    return "ok"


def readiness() -> dict[str, CheckResult]:
    database = check_database()
    # Skip the other checks when the database is down; they would only fail again.
    up = database == "ok"
    migrations: CheckResult = check_migrations() if up else "fail"
    role: CheckResult = check_database_role() if up else "fail"
    return {"database": database, "migrations": migrations, "database_role": role}


def _response(checks: dict[str, CheckResult]) -> JsonResponse:
    healthy = all(result == "ok" for result in checks.values())
    response = JsonResponse(
        {"status": "ok" if healthy else "fail", "checks": checks},
        status=200 if healthy else 503,
    )
    response["Cache-Control"] = "no-store"
    return response


class HealthCheckMiddleware:
    async_capable = True
    sync_capable = False

    def __init__(self, get_response: GetResponse) -> None:
        if not iscoroutinefunction(get_response):  # pragma: no cover - Django guarantees this
            msg = "HealthCheckMiddleware requires an async middleware chain"
            raise TypeError(msg)
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if request.path == LIVE_PATH and request.method in ("GET", "HEAD"):
            return _response({})
        if request.path == READY_PATH and request.method in ("GET", "HEAD"):
            return _response(await sync_to_async(readiness, thread_sensitive=True)())
        return await self.get_response(request)
