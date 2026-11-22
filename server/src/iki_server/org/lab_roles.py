"""Lab roles (ADR 0116): named roles such as Analyst, Reviewer and QA, which plugins declare and
a project's Admins give with memberships, beside their project role.

A lab role grants nothing by itself. A workflow approval, state change, signature or draw
purpose can require one, as can a field naming a person.
:mod:`iki_server.org.permissions` decides who holds one; this module knows which exist and
what needs them.
"""

from collections import Counter
from dataclasses import dataclass

from iki_spec.model import LabRole, is_lab_role, person_role

__all__ = ["Use", "declared", "label", "labels", "uses"]


def declared() -> dict[str, LabRole]:
    """Every lab role an installed plugin declares, by qualified name (``acme/qms.qa``)."""
    from iki_server.entities import registry

    out: dict[str, LabRole] = {}
    for plugin in registry.plugins().values():
        out.update(plugin.roles)
    return out


def labels() -> dict[str, str]:
    """What people call each declared lab role, by qualified name: its label, and where
    another plugin's role has the same one, its plugin's title after it, "QA (Lots)".

    A plugin can't give two of its own roles one label (`duplicate-role-label`), but it
    can't see other plugins' when it's checked. So two of them may each call a role QA,
    and wherever they're named -- the dialog that gives them, notifications, audit
    records, why a change was refused -- each says whose it is, as Slack and GitHub name
    an app beside a command that two apps share.
    """
    from iki_server.entities import registry

    plugins = registry.plugins()
    roles = {name: role for p in plugins.values() for name, role in p.roles.items()}
    shared = Counter(role.label.casefold() for role in roles.values())
    return {
        name: (
            f"{role.label} ({plugins[role.plugin].title or role.plugin})"
            if shared[role.label.casefold()] > 1
            else role.label
        )
        for name, role in roles.items()
    }


def label(role: str) -> str:
    """What people call a lab role (see :func:`labels`), or its name if no plugin declares
    it now."""
    return labels().get(role) or role.rpartition(".")[2]


@dataclass(frozen=True)
class Use:
    """Something a lab role lets its holders do."""

    #: ``approval``, a workflow step's; ``field``, a type's field naming people, who
    #: must hold it (ADR 0122); ``state``, a state field some of whose states its
    #: holders set (ADR 0125); ``signature``, one setting a state needs (ADR 0126);
    #: or ``draw``, an amount draw for a particular purpose (ADR 0139).
    kind: str
    #: The workflow or type it's in, qualified.
    owner: str
    #: The step, field or draw purpose it's for. A signature names its state field.
    name: str
    #: What it lets them do, in a sentence's words: "approve “Weigh” in Review, then weigh".
    description: str


def uses(role: str) -> list[Use]:
    """What a lab role lets its holders do, in the installed plugins."""
    from iki_server.amounts.models import DrawPurpose
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
            for purpose, allowed in entity_type.draw_purposes.items():
                if role in allowed:
                    purpose_label = DrawPurpose(purpose).label
                    out.append(
                        Use(
                            kind="draw",
                            owner=entity_type.qualified_name,
                            name=purpose,
                            description=f"record draws from {plural} as “{purpose_label}”",
                        )
                    )
    return out
