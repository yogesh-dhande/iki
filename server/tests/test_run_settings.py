"""Plugin settings an action can't run without (ADR 0169): until the deployment sets them,
the action isn't available, people are told why before they launch it, and a launch is
refused, rather than a worker taking the run only for it to fail at once."""

import dataclasses
from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as
from test_runs import COMPUTE, launch, world
from test_workers import keep_connection

from iki_server.entities import registry
from iki_server.runs import catalog
from iki_server.runs.models import ActionRun

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

UNSET = (
    "It needs the acme/protein plugin's `ncbi_email` setting, which this deployment hasn't "
    "set. An Iki administrator can set it in the plugin's settings, in the deployment's "
    "config repository."
)


@pytest.fixture
def needing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """COMPUTE, needing `ncbi_email`; the deployment's settings, for a test to fill in."""
    installed = catalog.actions()
    action = dataclasses.replace(installed[COMPUTE], requires_settings=("ncbi_email",))
    monkeypatch.setattr(catalog, "actions", lambda: {**installed, COMPUTE: action})
    values: dict[str, Any] = {"ncbi_email": None}
    real = registry.settings
    monkeypatch.setattr(
        registry, "settings", lambda plugin: values if plugin == "acme/protein" else real(plugin)
    )
    return values


def test_an_action_needing_a_setting_the_deployment_hasnt_set_cant_run(
    needing: dict[str, Any],
) -> None:
    action = catalog.action(COMPUTE)
    assert action is not None
    assert catalog.availability(action) == catalog.Availability(False, UNSET)
    # A blank value is no better than none.
    needing["ncbi_email"] = ""
    assert catalog.availability(action).reason == UNSET
    needing["ncbi_email"] = "lab@example.org"
    assert catalog.availability(action).available


def test_people_see_why_before_they_launch_and_a_launch_is_refused(
    world: World, needing: dict[str, Any]
) -> None:
    listed = {a["name"]: a for a in api_as(world, "viewer").every("/actions")}
    assert listed[COMPUTE]["available"] is False
    assert listed[COMPUTE]["unavailable_reason"] == UNSET
    editor = api_as(world, "editor")
    made = editor.post(
        "/entities",
        {
            "type": "acme/protein.Protein",
            "project": str(world.project.pk),
            # Human ubiquitin, UniProt P0CG48's first copy.
            "values": {
                "name": "UBB",
                "sequence": "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKES"
                "TLHLVLRLRGG",
            },
        },
    )
    assert made.status_code == 201, made.content
    refused = launch(editor, world.project, {"protein": made.json()["id"]})
    assert problem(refused, 409, "refused")["detail"] == UNSET
    assert not ActionRun.objects.exists()

    needing["ncbi_email"] = "lab@example.org"
    started = launch(editor, world.project, {"protein": made.json()["id"]})
    assert started.status_code == 201, started.content
    assert ActionRun.objects.count() == 1
