"""The ``/api/v1`` NinjaAPI: routers, error handling and the OpenAPI document.

The OpenAPI document is at ``/api/v1/openapi.json``. There is no bundled
Swagger UI: it loads scripts and styles that Iki's strict CSP doesn't allow,
and any OpenAPI viewer can read the document instead.
"""

from typing import Any

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from iki_server.api import (
    entities,
    files,
    me,
    notifications,
    projects,
    searches,
    service_accounts,
    teams,
    users,
    webhooks,
)
from iki_server.api.problems import Problem, from_validation, response
from iki_server.api.security import BearerAuth, SessionAuth
from iki_server.org.services import Refused

logger = structlog.get_logger("iki.api")

api = NinjaAPI(
    title="Iki API",
    version="1",
    description=(
        "Iki's REST API. Errors are RFC 9457 problem details; lists use cursors. "
        "Live updates (Server-Sent Events) come from GET /api/v1/events."
    ),
    urls_namespace="api-v1",
    auth=[BearerAuth(), SessionAuth()],
    docs_url=None,
    openapi_url="/openapi.json",
)

for module in (
    me,
    users,
    service_accounts,
    teams,
    projects,
    entities,
    searches,
    files,
    notifications,
    webhooks,
):
    api.add_router("", module.router)


@api.exception_handler(Problem)
def _problem(request: HttpRequest, exc: Problem) -> HttpResponse:
    return response(exc)


@api.exception_handler(ValidationError)
def _invalid(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    return response(from_validation(list(exc.errors)))


@api.exception_handler(AuthenticationError)
def _unauthenticated(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    return response(Problem(401))


@api.exception_handler(HttpError)
def _http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    if exc.status_code == 400:
        # django-ninja's only 400: a body it couldn't parse.
        return response(Problem(400, "The request body isn’t valid JSON.", code="invalid_json"))
    return response(Problem(exc.status_code, str(exc) or None))


@api.exception_handler(Refused)
def _refused(request: HttpRequest, exc: Refused) -> HttpResponse:
    errors = [{"pointer": f"#/{exc.field}", "detail": str(exc)}] if exc.field else None
    return response(Problem(409, str(exc), code="refused", errors=errors))


@api.exception_handler(Http404)
@api.exception_handler(ObjectDoesNotExist)
def _not_found(request: HttpRequest, exc: Exception) -> HttpResponse:
    return response(Problem(404))


@api.exception_handler(PermissionDenied)
def _forbidden(request: HttpRequest, exc: PermissionDenied) -> HttpResponse:
    return response(Problem(403))


@api.exception_handler(BotoCoreError)
@api.exception_handler(ClientError)
def _storage_unavailable(request: HttpRequest, exc: Exception) -> HttpResponse:
    logger.error("storage_error", error=str(exc), exc_info=exc)
    return response(
        Problem(
            503,
            "Iki can’t reach its file storage right now. Try again shortly.",
            code="storage_unavailable",
            headers={"Retry-After": "30"},
        )
    )


@api.exception_handler(Exception)
def _unexpected(request: HttpRequest, exc: Exception) -> Any:  # noqa: ANN401
    # Let Django log it and answer 500; ProblemMiddleware turns that into a problem.
    raise exc


urlpatterns = api.urls
