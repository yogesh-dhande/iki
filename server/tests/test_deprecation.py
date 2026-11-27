"""A deprecated REST operation says so: in the OpenAPI document, and in each response's
`Deprecation` and `Sunset` headers (ADR 0293)."""

from __future__ import annotations

from datetime import date

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from ninja import NinjaAPI

from iki_server.api.deprecation import Deprecation, deprecated, headers
from iki_server.api.middleware import ApiResponseMiddleware
from iki_server.api.v1 import api
from iki_server.api.worker import api as worker_api


@pytest.mark.parametrize("ninja", [api, worker_api], ids=["api", "worker"])
def test_an_operation_is_deprecated_in_the_document_exactly_when_it_says_when(
    ninja: NinjaAPI,
) -> None:
    for _, router in ninja._routers:
        for view in router.path_operations.values():
            for operation in view.operations:
                dated = getattr(operation.view_func, "iki_deprecation", None) is not None
                where = f"{operation.methods} {operation.view_func.__qualname__}"
                assert bool(operation.deprecated) == dated, (
                    f"{where}: pass deprecated=True to its router and decorate it with "
                    "@deprecated(...), or neither."
                )


def test_a_deprecated_operation_says_when_it_was_deprecated_and_when_it_goes() -> None:
    @deprecated(
        since="0.3",
        on=date(2027, 3, 1),
        sunset=date(2027, 6, 1),
        use="GET /api/v1/items",
        link="https://ikisci.org/docs/releases/0.3",
    )
    def things(request: HttpRequest) -> dict[str, str]:
        return {"ok": "yes"}

    request = RequestFactory().get("/api/v1/things")
    assert things(request) == {"ok": "yes"}
    response = ApiResponseMiddleware(lambda r: HttpResponse()).process_response(
        request, HttpResponse("{}", content_type="application/json")
    )
    # RFC 9745: seconds since the epoch, as a structured-field date.
    assert response["Deprecation"] == "@1803859200"
    # RFC 8594: an HTTP-date.
    assert response["Sunset"] == "Tue, 01 Jun 2027 00:00:00 GMT"
    assert response["Link"] == (
        '<https://ikisci.org/docs/releases/0.3>; rel="deprecation"; type="text/html"'
    )


def test_other_operations_send_no_deprecation_headers() -> None:
    request = RequestFactory().get("/api/v1/things")
    response = ApiResponseMiddleware(lambda r: HttpResponse()).process_response(
        request, HttpResponse("{}", content_type="application/json")
    )
    assert "Deprecation" not in response
    assert "Sunset" not in response


def test_a_sunset_comes_after_the_deprecation() -> None:
    with pytest.raises(ValueError, match="sunset comes after"):
        Deprecation(since="0.3", on=date(2027, 3, 1), sunset=date(2027, 3, 1), use="x")
    assert "Link" not in headers(
        Deprecation(since="0.3", on=date(2027, 3, 1), sunset=date(2027, 6, 1), use="x")
    )
