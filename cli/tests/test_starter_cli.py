"""Assay administrators use the same starter contracts from the CLI and the web app."""

import json
from typing import Any
from uuid import UUID

import pytest

from iki import Client, Forbidden
from iki_cli.main import main

PROJECT = str(UUID(int=1))
REQUESTER = str(UUID(int=2))
FIRST = str(UUID(int=3))
SECOND = str(UUID(int=4))
REVIEWER = "iki/plate_assays.reviewer"
RESULT = "iki/plate_assays.RequestedResearchAssayResult"
STARTER = {
    "id": "dose-response",
    "version": "1.0.0",
    "label": "Dose-response plate assay",
    "description": "Receive a request and fit a curve.",
    "flows": [{"id": "research", "label": "Research", "result_type": RESULT}],
    "roles": [{"id": REVIEWER, "label": "Assay reviewer"}],
    "types": [RESULT],
    "applied": {"version": "1.0.0", "flow": "research"},
}


@pytest.fixture(autouse=True)
def _signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IKI_URL", "https://service.example")
    monkeypatch.setenv("IKI_TOKEN", "test-api-sign-in")


@pytest.mark.parametrize("project", [None, PROJECT])
def test_list_installed_starters(
    project: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def request(client: Client, method: str, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert client.url == "https://service.example"
        assert (method, path) == ("GET", "/starters")
        assert kwargs["params"] == ({"project": project} if project else {})
        return [STARTER]

    monkeypatch.setattr(Client, "request", request)
    args = ["starter", "list", "--project", project] if project else ["starter", "list", "--json"]
    assert main(args) == 0
    output = capsys.readouterr().out
    if project:
        for item in ("dose-response 1.0.0", "Research", REVIEWER, RESULT, "Applied"):
            assert item in output
    else:
        assert json.loads(output) == [STARTER]


@pytest.mark.parametrize(
    ("command", "flow", "changed"),
    [("preview", "research", True), ("apply", "regulated_qc", True), ("apply", "research", False)],
)
def test_preview_and_apply_send_the_selected_version_scope_flow_and_members(
    command: str,
    flow: str,
    changed: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "version": "1.0.0",
        "project": PROJECT,
        "requester_project": REQUESTER,
        "flow": flow,
        "role_assignments": {REVIEWER: [FIRST, SECOND]},
    }
    response = {
        **payload,
        "starter": "dose-response",
        "result_type": RESULT,
        "changed": changed,
        "examples": {"plate": str(UUID(int=5))},
        "examples_to_create": ["request"] if changed else [],
        "grants_to_add": {REVIEWER: [SECOND]} if changed else {},
    }

    def request(client: Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert client.url == "https://other.example"
        assert (method, path) == ("POST", f"/starters/dose-response/{command}")
        assert kwargs["json"] == payload
        return response

    monkeypatch.setattr(Client, "request", request)
    args = [
        "starter",
        command,
        "dose-response",
        "--version",
        "1.0.0",
        "--project",
        PROJECT,
        "--requester-project",
        REQUESTER,
        "--flow",
        flow,
        "--role",
        f"{REVIEWER}={FIRST}",
        "--role",
        f"{REVIEWER}={SECOND}",
        "--url",
        "https://other.example",
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    status = (
        ("Would apply" if command == "preview" else "Applied") if changed else "Already applied"
    )
    for item in (
        status,
        "dose-response",
        "1.0.0",
        PROJECT,
        REQUESTER,
        flow,
        REVIEWER,
        FIRST,
        SECOND,
    ):
        assert item in out
    if command == "preview":
        assert "Create example request" in out
        assert f"Grant {REVIEWER} to {SECOND}" in out
    assert main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == response


def test_role_assignment_needs_a_membership_uuid(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "starter",
                "apply",
                "dose-response",
                "--version",
                "1.0.0",
                "--flow",
                "research",
                "--project",
                PROJECT,
                "--requester-project",
                REQUESTER,
                "--role",
                f"{REVIEWER}=handle",
            ]
        )
    assert exc.value.code == 2
    assert "membership UUID" in capsys.readouterr().err


def test_rejected_setup_displays_api_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(*args: Any, **kwargs: Any) -> None:
        raise Forbidden(403, "Requires a project administrator.")

    monkeypatch.setattr(Client, "request", denied)
    assert main(["starter", "list", "--project", PROJECT]) == 2
    assert "Requires a project administrator." in capsys.readouterr().err
