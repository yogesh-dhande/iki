"""Read-only schema change reports for organization administrators (ADR 0181)."""

from typing import Any, Literal

from django.http import HttpRequest
from ninja import Field, Router, Schema
from pydantic import ConfigDict

from iki_server.api.common import caller, require, require_scope
from iki_server.api.problems import Problem
from iki_server.audit import log as audit
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
    count: int = Field(description="Number of affected records. A sampled count is a lower bound.")
    sampled: bool = Field(description="Whether this finding counts only a sample of the records.")
    examples: list[str] = Field(description="Example record HRIDs.")


class PluginTypePreflightOut(Schema):
    type: str
    total: int = Field(description="Exact number of eligible records of this type.")
    checked: int = Field(
        description="Records checked by the sampled checks, or the total if complete."
    )
    sampled: bool = Field(description="Whether any checks had incomplete coverage.")
    sampled_checks: list[str] = Field(
        description="Checks limited to the sample, including checks with no findings."
    )
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
    The report gives exact impact counts where SQL can evaluate the change. It identifies checks
    that ran on a sample and the number of records they checked. Requires an Org Admin who can read
    every project, including archived projects. Tokens need read, admin, and access to all projects.
    """
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    require_scope(me, "read")
    try:
        report = impact.preview_manifest(me, name, payload.plugin)
    except impact.NeedsEveryProject as error:
        raise Problem(403, str(error), code="preflight_needs_every_project") from error
    except impact.PreflightError as error:
        raise Problem(409, str(error), code="preflight_unavailable") from error
    except ValueError as error:
        raise Problem(422, str(error), code="invalid_manifest") from error
    audit.record(
        "plugin.preflight",
        actor=me,
        data={
            "plugin": name,
            "candidate_revision": report.candidate_revision,
            "configuration_revision": report.configuration_revision,
        },
    )
    return report.as_dict()
