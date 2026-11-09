"""CSP violation reports: logged for operators, without secrets, and bounded."""

import json

import pytest
from django.test import Client
from structlog.testing import capture_logs

pytestmark = pytest.mark.django_db


def post(client: Client, body: object, content_type: str = "application/reports+json") -> int:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    response = client.post("/csp-report/", data, content_type=content_type, secure=True)
    return response.status_code


def test_report_to_reports_are_logged_without_query_strings() -> None:
    report = [
        {
            "type": "csp-violation",
            "body": {
                "effectiveDirective": "script-src-elem",
                "blockedURL": "https://evil.example/x.js?session=secret",
                "documentURL": "https://iki.example.com/setup/?token=secret#frag",
                "disposition": "enforce",
            },
        }
    ]
    with capture_logs() as logs:
        assert post(Client(), report) == 204
    [event] = [e for e in logs if e["event"] == "csp_violation"]
    assert event["directive"] == "script-src-elem"
    assert event["blocked"] == "https://evil.example/x.js"
    assert event["document"] == "https://iki.example.com/setup/"
    assert "secret" not in json.dumps(logs)


def test_legacy_report_uri_reports_are_understood() -> None:
    report = {"csp-report": {"violated-directive": "style-src", "blocked-uri": "inline"}}
    with capture_logs() as logs:
        assert post(Client(), report, "application/csp-report") == 204
    [event] = [e for e in logs if e["event"] == "csp_violation"]
    assert (event["directive"], event["blocked"]) == ("style-src", "inline")


def test_oversized_and_malformed_reports_are_refused() -> None:
    assert post(Client(), b"x" * (16 * 1024 + 1)) == 413
    assert post(Client(), b"{not json") == 400


def test_reports_are_rate_limited() -> None:
    client = Client()
    statuses = [post(client, []) for _ in range(31)]
    assert statuses[:30] == [204] * 30
    assert statuses[30] == 429


def test_only_post_is_accepted() -> None:
    assert Client().get("/csp-report/", secure=True).status_code == 405
