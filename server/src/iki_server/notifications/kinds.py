"""The kinds of notification, which people choose email for one by one.

Core registers its kinds here; plugins register theirs with :func:`register`
when their app loads. A kind's key never changes once released: people's
email choices are stored against it.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


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
    try:
        return KINDS[key]
    except KeyError:
        raise ValueError(f"No notification kind {key!r}. Register it first.") from None


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
WEBHOOK_DISABLED = register(
    Kind(
        "webhook.disabled",
        "A webhook was turned off",
        "Iki turns off a webhook you manage after its deliveries fail for days.",
        group="Webhooks",
    )
)
