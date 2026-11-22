"""Read-only schema change reports for organization administrators (ADR 0181)."""

from typing import Any, Literal

from django.http import HttpRequest
from ninja import Field, Router, Schema
from pydantic import ConfigDict

from iki_server.api.common import caller, require
from iki_server.api.problems import Problem
from iki_server.org.permissions import Action
from iki_server.plugins import preflight as impact

router = Router(tags=["plugins"])


class PluginCandidateIn(Schema):
    model_config = ConfigDict(extra="forbid")
    api_version: Literal["iki/v1"] = Field(alias="apiVersion")
    plugin: dict[str, Any] = Field(description="The plugin object in the built manifest.json.")


class PluginFindingOut(Schema):
    kind: str
    subject: str
    message: str
    blocking: bool
    count: int = Field(description="Affected records among those checked.")
    examples: list[str] = Field(description="Example record HRIDs.")


class PluginTypePreflightOut(Schema):
    type: str
    checked: int
    sampled: bool
    findings: list[PluginFindingOut]


class PluginPreflightOut(Schema):
    plugin: str
    version: str
    configuration_revision: str = Field(
        description="Revision of the deployed configuration checked."
    )
    candidate_revision: str = Field(description="SHA-256 of the proposed manifest.")
    blocking: bool
    types: list[PluginTypePreflightOut]


@router.post(
    "/plugins/{plugin:name}/preflight",
    response=PluginPreflightOut,
    summary="Check a candidate plugin against existing records",
)
def preflight(request: HttpRequest, name: str, payload: PluginCandidateIn) -> dict[str, Any]:
    """Send the proposed package's manifest.json from `iki plugin build` or `iki deploy build`.
    The report reads current records and identifies the sampled types. It requires an Org Admin
    and a token's admin scope with access to all projects."""
    require(caller(request), Action.ORG_MANAGE)
    try:
        return impact.preview_manifest(name, payload.plugin).as_dict()
    except impact.PreflightError as error:
        raise Problem(409, str(error), code="preflight_unavailable") from error
    except ValueError as error:
        raise Problem(422, str(error), code="invalid_manifest") from error
