"""Assay administrators use the same starter contracts from the CLI and the web app."""

import json
from typing import Any
from uuid import UUID

import pytest

from iki import Client, Conflict, Forbidden
from iki_cli.main import main

PROJECT = str(UUID(int=1))
FIRST = str(UUID(int=3))
SECOND = str(UUID(int=4))
REVIEWER = "iki/plate_assays.reviewer"
RESULTS = {
    "author_signed": "iki/experiments.Experiment",
    "research": "iki/plate_assays.ResearchAssayResult",
    "regulated_qc": "iki/plate_assays.QCAssayResult",
}
PLAN = "3f" * 32
STARTER = {
    "id": "dose-response",
    "version": "2.0.0",
    "label": "Dose-response plate assay",
    "description": "Configure an assay review flow and assign its project roles.",
    "flows": [
        {
            "id": "research",
            "label": "Research",
            "result_type": RESULTS["research"],
            "summary": "A result is recorded, then one reviewer signs it off.",
        }
    ],
    "roles": [{"id": REVIEWER, "label": "Assay reviewer"}],
    "types": [RESULTS["research"], RESULTS["regulated_qc"]],
    "applied": {"version": "2.0.0", "flow": "research"},
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
        for item in (
            "dose-response 2.0.0",
            "Research",
            "one reviewer signs it off",
            REVIEWER,
            RESULTS["research"],
            "Applied",
        ):
            assert item in output
    else:
        assert json.loads(output) == [STARTER]


@pytest.mark.parametrize(
    ("command", "flow", "changed"),
    [
        ("preview", "research", True),
        ("preview", "author_signed", True),
        ("apply", "regulated_qc", True),
        ("apply", "research", False),
    ],
)
def test_preview_and_apply_send_the_selected_version_scope_flow_and_members(
    command: str,
    flow: str,
    changed: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = {
        "version": "2.0.0",
        "project": PROJECT,
        "flow": flow,
        "role_assignments": {REVIEWER: [FIRST, SECOND]},
    }
    # Applying sends the plan its preview printed: what was reviewed.
    payload = {**setup, "plan": PLAN} if command == "apply" else setup
    response = {
        **setup,
        "plan": PLAN,
        "starter": "dose-response",
        "result_type": RESULTS[flow],
        "changed": changed,
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
        "2.0.0",
        "--project",
        PROJECT,
        "--flow",
        flow,
        "--role",
        f"{REVIEWER}={FIRST}",
        "--role",
        f"{REVIEWER}={SECOND}",
        "--url",
        "https://other.example",
        *(["--plan", PLAN] if command == "apply" else []),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    status = (
        ("Would apply" if command == "preview" else "Applied") if changed else "Already applied"
    )
    for item in (
        status,
        "dose-response",
        "2.0.0",
        PROJECT,
        flow,
        RESULTS[flow],
        REVIEWER,
        FIRST,
        SECOND,
    ):
        assert item in out
    if command == "preview":
        assert f"Grant {REVIEWER} to {SECOND}" in out
        assert f"Plan              {PLAN}" in out
    assert main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == response


def test_apply_needs_the_reviewed_plan(capsys: pytest.CaptureFixture[str]) -> None:
    base = [
        "starter",
        "apply",
        "dose-response",
        "--version",
        "2.0.0",
        "--flow",
        "research",
        "--project",
        PROJECT,
    ]
    for extra in ([], ["--plan", "latest"]):
        with pytest.raises(SystemExit) as exc:
            main([*base, *extra])
        assert exc.value.code == 2
    assert "--plan" in capsys.readouterr().err


def test_a_changed_plan_applies_nothing_and_shows_what_applying_does_now(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = {
        "starter": "dose-response",
        "version": "2.0.0",
        "project": PROJECT,
        "flow": "research",
        "result_type": RESULTS["research"],
        "changed": True,
        "role_assignments": {REVIEWER: [FIRST]},
        "grants_to_add": {REVIEWER: [FIRST]},
        "plan": "4a" * 32,
    }

    def changed(*args: Any, **kwargs: Any) -> None:
        detail = "What applying does changed since it was previewed."
        problem = {"status": 409, "code": "starter_plan_changed", "detail": detail, "plan": now}
        raise Conflict(409, detail, code="starter_plan_changed", problem=problem)

    monkeypatch.setattr(Client, "request", changed)
    args = [
        "starter",
        "apply",
        "dose-response",
        "--version",
        "2.0.0",
        "--flow",
        "research",
        "--project",
        PROJECT,
        "--role",
        f"{REVIEWER}={FIRST}",
        "--plan",
        PLAN,
    ]
    assert main(args) == 2
    err = capsys.readouterr().err
    assert "changed since it was previewed" in err
    assert f"Grant {REVIEWER} to {FIRST}" in err and f"Plan              {'4a' * 32}" in err


def test_role_assignment_needs_a_membership_uuid(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "starter",
                "apply",
                "dose-response",
                "--version",
                "2.0.0",
                "--flow",
                "research",
                "--project",
                PROJECT,
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
