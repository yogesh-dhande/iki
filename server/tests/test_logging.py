import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from django.test import AsyncClient
from opentelemetry import trace

from iki_server.observability.logging import build_logging_config, configure_logging
from iki_server.observability.middleware import REQUEST_ID_HEADER

pytestmark = pytest.mark.django_db


class JsonLines:
    def __init__(self) -> None:
        self.stream = io.StringIO()

    def read(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]


@pytest.fixture
def json_lines() -> Iterator[JsonLines]:
    lines = JsonLines()
    configure_logging("INFO", "json", stream=lines.stream)
    yield lines
    configure_logging("INFO", "json")


def test_stdlib_and_structlog_records_render_as_json(json_lines: JsonLines) -> None:
    logging.getLogger("django.db").warning("from stdlib %s", "args")
    structlog.get_logger("iki.test").info("from structlog", answer=42)
    stdlib, ours = json_lines.read()
    assert stdlib["event"] == "from stdlib args"
    assert stdlib["level"] == "warning"
    assert stdlib["logger"] == "django.db"
    assert ours["event"] == "from structlog"
    assert ours["answer"] == 42
    assert str(ours["timestamp"]).endswith("Z")


def test_log_lines_carry_the_active_trace_and_span(json_lines: JsonLines) -> None:
    with trace.get_tracer("test").start_as_current_span("work") as span:
        structlog.get_logger("iki.test").info("inside span")
    (line,) = json_lines.read()
    ctx = span.get_span_context()
    assert line["trace_id"] == format(ctx.trace_id, "032x")
    assert line["span_id"] == format(ctx.span_id, "016x")


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="IKI_LOG_FORMAT"):
        build_logging_config("INFO", "xml")


async def test_each_request_logs_one_line_without_query_string(
    async_client: AsyncClient, json_lines: JsonLines
) -> None:
    response = await async_client.get("/healthz/live?token=secret", secure=True)
    assert response.status_code == 200
    # Health checks short-circuit before the request log, so use a normal path.
    response = await async_client.get("/nope?code=secret-oauth-code", secure=True)
    assert response.status_code == 404
    lines = [line for line in json_lines.read() if line["event"] == "request"]
    assert len(lines) == 1
    (line,) = lines
    assert line["path"] == "/nope"
    assert line["status"] == 404
    assert line["method"] == "GET"
    assert isinstance(line["duration_ms"], float)
    assert "secret" not in json.dumps(line)
    assert line["request_id"] == response[REQUEST_ID_HEADER]


@pytest.mark.parametrize(
    ("incoming", "kept"),
    [
        ("abc-123_x.y", True),
        ("bad\nvalue", False),
        ("x" * 129, False),
        ("", False),
    ],
)
async def test_request_id_header_is_accepted_only_when_well_formed(
    async_client: AsyncClient, incoming: str, kept: bool
) -> None:
    response = await async_client.get("/nope", headers={REQUEST_ID_HEADER: incoming}, secure=True)
    assert (response[REQUEST_ID_HEADER] == incoming) is kept
    assert len(response[REQUEST_ID_HEADER]) <= 128
