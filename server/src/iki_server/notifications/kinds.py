"""The kinds of notification, which people choose how they're told of one by one.

Core registers its kinds here; plugins register theirs with :func:`register`
when their app loads. A kind's key never changes once released: people's
choices are stored against it.

Each notification a type declares (ADR 0129) is a kind too, keyed by its type and name
(``iki/example_lims.Sample.in_review``), read from the installed plugins: :func:`get` and
:func:`every` find them beside core's.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache


class Audience(StrEnum):
    EVERYONE = "everyone"
    #: Only Org Admins receive it, so only they see it in their preferences.
    ORG_ADMINS = "org_admins"


@dataclass(frozen=True)
class Kind:
    key: str
    #: How the preferences page names it: "Added to a project".
    label: str
    #: When it's sent, in a sentence.
    description: str
    #: Section on the preferences page.
    group: str
    #: Emailed unless the person turns it off.
    email: bool = True
    audience: Audience = Audience.EVERYONE
    #: For a type's declared notification (ADR 0129): the type, qualified, and its name.
    type: str = ""
    name: str = ""
    #: A declared notification nobody can turn off in Iki.
    required: bool = False

    @property
    def declared(self) -> bool:
        return bool(self.type)

    @property
    def can_turn_off(self) -> bool:
        """Whether someone can choose not to be told at all: only of a type's notification
        that isn't required. Core's are about the person themselves, and few."""
        return self.declared and not self.required


KINDS: dict[str, Kind] = {}
_KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def register(kind: Kind) -> Kind:
    if not _KEY.match(kind.key):
        raise ValueError(f"Notification kinds look like plugin.event, not {kind.key!r}.")
    existing = KINDS.get(kind.key)
    if existing is not None and existing != kind:
        raise ValueError(f"The notification kind {kind.key!r} is already registered.")
    KINDS[kind.key] = kind
    return kind


def get(key: str) -> Kind:
    found = KINDS.get(key) or declared().get(key)
    if found is None:
        raise ValueError(f"No notification kind {key!r}. Register it first.")
    return found


def find(key: str) -> Kind | None:
    """The kind, if it's registered or a type declares it."""
    return KINDS.get(key) or declared().get(key)


def every() -> list[Kind]:
    """Core's kinds in the order registered, then the types' declared ones, by type."""
    return [*KINDS.values(), *declared().values()]


def key_of(type_name: str, name: str) -> str:
    """A declared notification's kind: its type's qualified name, a dot, and its name."""
    return f"{type_name}.{name}"


@cache
def declared() -> dict[str, Kind]:
    """The installed types' declared notifications (ADR 0129), as kinds, by key: each type's
    own, grouped by type. A subtype's records send its ancestors' too, as theirs."""
    from iki_server.entities import registry

    plugins = registry.plugins()
    out: dict[str, Kind] = {}
    for name, entity_type in sorted(registry.types().items()):
        if not entity_type.notifications:
            continue
        plugin = plugins.get(entity_type.plugin)
        title = (plugin.title if plugin is not None else "") or entity_type.plugin
        group = f"{entity_type.label_plural or entity_type.label or entity_type.name} ({title})"
        for n in entity_type.notifications.values():
            key = key_of(name, n.name)
            out[key] = Kind(
                key,
                n.label,
                n.description or "",
                group=group,
                email=n.email,
                type=name,
                name=n.name,
                required=n.required,
            )
    return out


PROJECT_ADDED = register(
    Kind(
        "project.added",
        "Added to a project",
        "Someone gives you a role in a project.",
        group="Projects",
    )
)
PROJECT_ROLE_CHANGED = register(
    Kind(
        "project.role_changed",
        "Your role in a project changed",
        "Someone changes the role you have in a project.",
        group="Projects",
    )
)
PROJECT_REMOVED = register(
    Kind(
        "project.removed",
        "Removed from a project",
        "Someone removes you from a project.",
        group="Projects",
    )
)
LAB_ROLES_CHANGED = register(
    Kind(
        "project.lab_roles_changed",
        "Your lab roles in a project changed",
        "Someone gives you a lab role in a project, such as QA, or takes one away.",
        group="Projects",
    )
)
ASSIGNED = register(
    Kind(
        "assignment.assigned",
        "Assigned a record",
        "Someone names you in a record's assignment field, such as a sample's analyst, and "
        "it's your work in its state (ADR 0122).",
        group="Work",
    )
)
UNASSIGNED = register(
    Kind(
        "assignment.unassigned",
        "Taken off a record",
        "Someone takes you off a record you were assigned, or names someone else.",
        group="Work",
        email=False,
    )
)
READY = register(
    Kind(
        "assignment.ready",
        "A record is ready for you",
        "A record you're assigned reaches a state where it's your work: a sample you review "
        "comes into review, say.",
        group="Work",
    )
)
NEEDS_REASSIGNING = register(
    Kind(
        "assignment.needs_reassigning",
        "Records need reassigning",
        "In a project you're an Admin of, someone assigned records can't be any more: they "
        "left it, lost the lab role, or were deactivated.",
        group="Work",
    )
)
TEAM_ADDED = register(
    Kind("team.added", "Added to a team", "Someone adds you to a team.", group="Teams")
)
TEAM_REMOVED = register(
    Kind("team.removed", "Removed from a team", "Someone removes you from a team.", group="Teams")
)
APP_AWAITING_APPROVAL = register(
    Kind(
        "oauth.app_awaiting_approval",
        "An app is waiting for approval",
        "Someone asks for an app to be approved for the organization.",
        group="Administration",
        audience=Audience.ORG_ADMINS,
    )
)
RUN_APPROVAL_REQUESTED = register(
    Kind(
        "run.approval_requested",
        "An AI agent or a trigger asks to run something",
        "An app acting for you, such as an AI agent, or a trigger you turned on, asks to start "
        "runs that need your approval: on the container or VM tier.",
        group="Runs",
    )
)
WORKFLOW_APPROVAL_REQUESTED = register(
    Kind(
        "workflow.approval_requested",
        "A workflow step waits for approval",
        "A step of a workflow someone else started waits for a person with your role in its "
        "project, or your lab role, to approve it.",
        group="Runs",
    )
)
ENTRY_SUPERSEDED = register(
    Kind(
        "registry.entry_superseded",
        "A registry entry was superseded",
        "A registrar supersedes a registry entry with another, and a project you're an Admin "
        "of, or whose record of it you added, has a record of it to move.",
        group="Registries",
    )
)
TRIGGER_TURNED_OFF = register(
    Kind(
        "trigger.turned_off",
        "A trigger was turned off",
        "A trigger in a project you're an Admin of is turned off because its runs can no longer "
        "be launched as the Admin who turned it on.",
        group="Runs",
    )
)
WORKFLOW_STEP_DECLINED = register(
    Kind(
        "workflow.step_declined",
        "A step of your workflow was declined",
        "A step of a workflow you started is declined, or nobody approves it in time, so the "
        "workflow fails.",
        group="Runs",
    )
)
BUDGET_THRESHOLD = register(
    Kind(
        "budget.threshold_reached",
        "A budget is being spent",
        "Spending on runs passes one of a budget's warning levels for the month: for "
        "Compute Admins, and a project's Admins for its own budget.",
        group="Costs",
    )
)
WEBHOOK_DISABLED = register(
    Kind(
        "webhook.disabled",
        "A webhook was turned off",
        "Iki turns off a webhook you manage after its deliveries fail for days.",
        group="Webhooks",
    )
)
