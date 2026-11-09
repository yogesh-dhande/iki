"""Request logging middleware."""

import re
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponseBase

logger = structlog.get_logger("iki.request")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

GetResponse = Callable[[HttpRequest], Awaitable[HttpResponseBase]]


def request_id_for(request: HttpRequest) -> str:
    """Returns the caller's ``X-Request-ID`` if it is well formed, otherwise a new one.

    The format is restricted so a caller can't inject newlines or control
    characters into logs.
    """
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    if _VALID_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestLogMiddleware:
    """Logs one line per request and tags every log line inside it with a request ID.

    Only the path is logged, never the query string, because query strings
    can carry secrets such as OAuth codes or presigned-URL signatures.
    """

    async_capable = True
    sync_capable = False

    def __init__(self, get_response: GetResponse) -> None:
        if not iscoroutinefunction(get_response):  # pragma: no cover - Django guarantees this
            msg = "RequestLogMiddleware requires an async middleware chain"
            raise TypeError(msg)
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request: HttpRequest) -> HttpResponseBase:
        request_id = request_id_for(request)
        start = time.perf_counter()
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                response = await self.get_response(request)
            except Exception:  # pragma: no cover - Django converts exceptions to 500s
                logger.exception("request failed", method=request.method, path=request.path)
                raise
            response[REQUEST_ID_HEADER] = request_id
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log = logger.warning if response.status_code >= 500 else logger.info
            log(
                "request",
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=duration_ms,
                client=request.META.get("REMOTE_ADDR"),
            )
        return response
