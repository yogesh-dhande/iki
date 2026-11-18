"""Lab roles (ADR 0116): named roles such as Analyst, Reviewer and QA, which plugins declare and
a project's Admins give with memberships, beside their project role.

A lab role grants nothing by itself. A workflow step's approval can require one, and so can
a record's transitions, which is what they're for. :mod:`iki_server.org.permissions` decides
who holds one; this module knows which exist and what needs them.
"""

from dataclasses import dataclass

from iki_spec.model import LabRole, is_lab_role

__all__ = ["Use", "declared", "label", "uses"]


def declared() -> dict[str, LabRole]:
    """Every lab role an installed plugin declares, by qualified name (``acme/qms.qa``)."""
    from iki_server.entities import registry

    out: dict[str, LabRole] = {}
    for plugin in registry.plugins().values():
        out.update(plugin.roles)
    return out


def label(role: str) -> str:
    """What people call a lab role: its label, or its name if no plugin declares it now."""
    found = declared().get(role)
    return found.label if found is not None else role.rpartition(".")[2]


@dataclass(frozen=True)
class Use:
    """Something a lab role lets its holders do."""

    #: ``approval``, a workflow step's (and ``transition``, a type's, with record states).
    kind: str
    #: The workflow (or type) it's in, qualified.
    owner: str
    #: The step (or transition) it's for.
    name: str
    #: What it lets them do, in a sentence's words: "approve “Weigh” in Review, then weigh".
    description: str


def uses(role: str) -> list[Use]:
    """What a lab role lets its holders do, in the installed plugins."""
    from iki_server.entities import registry

    out: list[Use] = []
    for plugin in registry.plugins().values():
        for workflow in plugin.workflows.values():
            for step in workflow.steps.values():
                approval = step.approval
                if approval is not None and is_lab_role(approval.role) and approval.role == role:
                    out.append(
                        Use(
                            kind="approval",
                            owner=workflow.qualified_name,
                            name=step.id,
                            description=f"approve “{step.label}” in {workflow.label}",
                        )
                    )
    return out
