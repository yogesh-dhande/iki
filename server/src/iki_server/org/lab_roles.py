"""Lab roles (ADR 0116): named roles such as Analyst, Reviewer and QA, which plugins declare and
a project's Admins give with memberships, beside their project role.

A lab role grants nothing by itself. A workflow step's approval can require one, and each of
a state field's states names those that set it (ADR 0125), which is what they're for.
:mod:`iki_server.org.permissions` decides who holds one; this module knows which exist and
what needs them.
"""

from dataclasses import dataclass

from iki_spec.model import LabRole, is_lab_role, person_role

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

    #: ``approval``, a workflow step's; ``field``, a type's field naming people, who
    #: must hold it (ADR 0122); ``state``, a state field some of whose states its
    #: holders set (ADR 0125); or ``signature``, one setting a state needs (ADR 0126).
    kind: str
    #: The workflow or type it's in, qualified.
    owner: str
    #: The step or field it's for: for a signature, its state field.
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
        for entity_type in plugin.types.values():
            plural = (entity_type.label_plural or entity_type.name).lower()
            for name, f in entity_type.fields.items():
                if not f.removed and person_role(f) == role:
                    out.append(
                        Use(
                            kind="field",
                            owner=entity_type.qualified_name,
                            name=name,
                            description=f"be named {f.label or name} on {plural}",
                        )
                    )
            for f in entity_type.fields.values():
                if f.type != "state" or f.removed:
                    continue
                # One use per state field, naming the states the role sets.
                labels = [s.label for s in f.states.values() if not s.removed and role in s.set_by]
                if not labels:
                    continue
                either = labels[0] if len(labels) == 1 else ", ".join(labels[:-1])
                either += f" or {labels[-1]}" if len(labels) > 1 else ""
                out.append(
                    Use(
                        kind="state",
                        owner=entity_type.qualified_name,
                        name=f.name,
                        description=f"set {f.label} to {either} on {plural}",
                    )
                )
            # The electronic signatures its states need (ADR 0126): a signature's own role,
            # or one of its state's.
            for f in entity_type.fields.values():
                if f.type != "state" or f.removed:
                    continue
                out.extend(
                    Use(
                        kind="signature",
                        owner=entity_type.qualified_name,
                        name=f.name,
                        description=f"sign “{wanted.label}” to set {f.label} to {state.label} "
                        f"on {plural}",
                    )
                    for state in f.states.values()
                    if not state.removed
                    for wanted in state.signatures
                    if role in ((wanted.role,) if wanted.role else state.set_by)
                )
    return out
