"""Project review choices at run launch and record creation (ADRs 0198 and 0240)."""

from collections.abc import Iterable

from django.db import connection

from iki_server.entities import registry
from iki_server.org.models import Project


def _hold_choice(project: Project) -> None:
    # An application, including the first, locks this same project FOR UPDATE.
    # Creates in the same project may proceed together under FOR SHARE.
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM iki_project WHERE id = %s FOR SHARE", [project.pk])


def run_settings(project: Project, plugin: str) -> dict[str, str]:
    """The starter's setting overrides for this project's run, pinned at launch."""
    from iki_server.setup.starters import CATALOG, effective_flow

    overrides = {}
    for starter in CATALOG:
        if starter.plugin == plugin:
            _hold_choice(project)
            if choice := effective_flow(project, starter.id):
                overrides[starter.setting] = choice
    return overrides


def refused_create(project: Project, types: Iterable[str]) -> str | None:
    """Check new records under the project lock, held until the create commits.

    A concrete result (including a lab's subtype) carries its flow for life.
    Only new records consult the project's current choice. A starter whose flow is
    pinned on an opening record leaves its later records to that pin (ADR 0240).
    """
    from iki_server.setup.starters import CATALOG, effective_flow

    for name in types:
        bases = {name, *(t.qualified_name for t in registry.ancestors(registry.types()[name]))}
        for starter in CATALOG:
            if starter.pinned_by is None and starter.result_base in bases:
                _hold_choice(project)
                flow = effective_flow(project, starter.id)
                if flow is not None and starter.flows[flow] not in bases:
                    return (
                        f"The {starter.id} starter in this project uses {flow}. Create "
                        f"{starter.flows[flow]} or one of its subtypes. A fit started "
                        "before the flow changed must be run again."
                    )
    return None
