"""What the API sends and accepts."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from ninja import Field, Schema

from iki_server.accounts.models import User
from iki_server.org.models import OrgRole, Project, ProjectMembership, ProjectRole, Team

ProjectRoleName = Literal["viewer", "editor", "runner", "admin"]
OrgRoleName = Literal["org_admin", "compute_admin", "auditor"]
KindName = Literal["person", "service"]


def role_name(role: int | None) -> ProjectRoleName | None:
    return None if role is None else ProjectRole(role).name.lower()  # type: ignore[return-value]


def parse_role(name: ProjectRoleName) -> ProjectRole:
    return ProjectRole[name.upper()]


class Page[T](Schema):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as cursor for the next page; null at the end."
    )


class UserOut(Schema):
    id: UUID
    email: str = Field(description="For service accounts, an address that receives no mail.")
    name: str
    kind: KindName
    active: bool = Field(alias="is_active")


class AgentReportedOut(Schema):
    software: str | None = Field(
        description="What software the agent says it is (MCP's clientInfo). Iki can't check it."
    )
    model: str | None = Field(
        description="What model the agent says it is (its Iki-Agent-Model header). Iki can't "
        "check it."
    )


class AgentOut(Schema):
    """The agent that acted for the person (ADR 0072): an OAuth client, through one of
    the person's grants."""

    client_id: str | None = Field(
        description="The OAuth client's ID. Null for older records whose label doesn't name it."
    )
    name: str = Field(description="The client's name when it acted.")
    grant: UUID | None = Field(
        description="The person's grant to the client it acted under, if recorded."
    )
    mcp_session: str | None = Field(description="The MCP session, if it came through one.")
    reported: AgentReportedOut


def agent_out(agent: Any, label: str = "") -> dict[str, Any] | None:  # noqa: ANN401 - an Agent
    """A record's agent, or for an older record, what its client label says."""
    from iki_server.audit.log import legacy_agent

    if agent is not None:
        return {
            "client_id": agent.client_id,
            "name": agent.client_name,
            "grant": agent.grant,
            "mcp_session": agent.session or None,
            "reported": {"software": agent.software or None, "model": agent.model or None},
        }
    if not label:
        return None
    name, client_id = legacy_agent(label)
    return {
        "client_id": client_id,
        "name": name,
        "grant": None,
        "mcp_session": None,
        "reported": {"software": None, "model": None},
    }


class MeCan(Schema):
    manage_org: bool = Field(description="People, teams, sign-in and apps settings.")
    read_audit: bool
    create_projects: bool = Field(description="In at least one of your teams.")
    manage_webhooks: bool = Field(description="The organization's webhooks.")
    read_costs: bool = Field(description="What runs cost, across the organization.")
    manage_compute: bool = Field(description="Workers, quotas and budgets.")


class OrgRolesIn(Schema):
    roles: list[OrgRoleName]


class OrgRolesOut(Schema):
    roles: list[OrgRoleName]


class UserPatch(Schema):
    active: bool | None = None


class PersonRef(Schema):
    """Someone, by name, or by email address when they have none."""

    id: UUID
    name: str

    @staticmethod
    def resolve_name(person: User) -> str:
        return person.get_full_name()


class MeDelegationOut(Schema):
    """A person's delegation a token acts under (ADR 0073)."""

    service_account: PersonRef = Field(description="The service account whose agent acts.")
    expires_at: datetime = Field(description="When the delegation, and its tokens, stop.")


class MeOut(UserOut):
    org_roles: list[OrgRoleName]
    can: MeCan
    agent: AgentOut | None = Field(
        description="The agent this token acts through (ADR 0072), if any. Null in the web app."
    )
    delegation: MeDelegationOut | None = Field(
        description="For a token an agent got by acting for you (ADR 0073), your delegation "
        "to it. Null otherwise."
    )


class ProjectRef(Schema):
    id: UUID
    name: str
    slug: str


class TeamRef(Schema):
    id: UUID
    name: str
    slug: str


class TeamOut(TeamRef):
    description: str
    archived: bool = Field(alias="is_archived")
    created_at: datetime
    linked_groups: list[str] = Field(
        description="Identity provider groups that decide the members, if any."
    )

    @staticmethod
    def resolve_linked_groups(team: Team) -> list[str]:
        cached = getattr(team, "linked_group_names", None)
        if cached is not None:
            return list(cached)
        from iki_server.org.services import linked_groups

        return linked_groups(team)


class TeamIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=1000)


class TeamPatch(Schema):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=1000)
    archived: bool | None = None


class TeamMemberOut(Schema):
    user: UserOut
    added_at: datetime


class ProjectCan(Schema):
    read: bool = Field(description="Read the project's data.")
    edit: bool = Field(description="Add entities and files, and change its entities.")
    manage: bool = Field(
        description="Rename, describe, archive or restore it, and allow or restrict sharing."
    )
    manage_members: bool = Field(description="Give and remove access.")
    manage_webhooks: bool = Field(description="Add and change the project's webhooks.")
    run: list[Literal["client", "backend", "container", "vm"]] = Field(
        description="The tiers whose actions you may run here, and whose runs you may cancel. "
        "None while it's archived."
    )


class ProjectOut(Schema):
    id: UUID
    name: str
    slug: str
    description: str
    team: TeamRef
    archived: bool = Field(alias="is_archived")
    allow_sharing: bool = Field(
        description="Whether its entities may be shared with other projects: linked into "
        "them, used in their runs, referred to by their entities, or reused as their "
        "duplicates. Off for confidential work (ADR 0067)."
    )
    created_at: datetime
    my_role: ProjectRoleName | None = Field(
        description="Your role, from your own grant or your teams'; null if you have none."
    )
    can: ProjectCan = Field(description="What you may do with it.")

    @staticmethod
    def resolve_my_role(project: object) -> ProjectRoleName | None:
        return role_name(getattr(project, "my_role", None))

    @staticmethod
    def resolve_can(project: Project, context: dict[str, Any]) -> dict[str, Any]:
        from iki_server.org import permissions
        from iki_server.org.permissions import Action
        from iki_server.runs.services import TIER_PERMISSIONS

        user = context["request"].auth
        return {
            "read": permissions.can(user, Action.PROJECT_READ, project),
            "edit": permissions.can(user, Action.PROJECT_EDIT, project),
            "manage": permissions.can(user, Action.PROJECT_MANAGE, project),
            "manage_members": permissions.can(user, Action.PROJECT_MEMBERS_MANAGE, project),
            "manage_webhooks": permissions.can(user, Action.PROJECT_WEBHOOKS_MANAGE, project),
            "run": []
            if project.is_archived
            else [t for t, a in TIER_PERMISSIONS.items() if permissions.can(user, a, project)],
        }


class ProjectIn(Schema):
    team: UUID
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=2000)


class ProjectPatch(Schema):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    archived: bool | None = None
    allow_sharing: bool | None = Field(
        None,
        description="Turning it off keeps entities already shared with other projects there, "
        "until someone takes them out. It can change while the project is archived.",
    )


class ProjectMemberOut(Schema):
    id: UUID
    role: ProjectRoleName
    user: UserOut | None
    team: TeamRef | None
    team_size: int | None = Field(description="For a team's grant, how many people it covers.")
    added_at: datetime

    @staticmethod
    def resolve_team_size(membership: ProjectMembership) -> int | None:
        team = membership.team
        if team is None:
            return None
        cached = getattr(membership, "team_size", None)
        return int(cached) if cached is not None else team.memberships.count()

    @staticmethod
    def resolve_role(membership: ProjectMembership) -> ProjectRoleName | None:
        return role_name(membership.role)


class ProjectRoleOut(Schema):
    name: ProjectRoleName
    label: str
    description: str


class RoleIn(Schema):
    role: ProjectRoleName


class ServiceAccountOut(UserOut):
    description: str
    org_roles: list[OrgRoleName]
    created_at: datetime
    created_by: UserOut | None

    @staticmethod
    def resolve_description(user: User) -> str:
        return user.service_account.description

    @staticmethod
    def resolve_created_at(user: User) -> datetime:
        return user.service_account.created_at

    @staticmethod
    def resolve_created_by(user: User) -> User | None:
        return user.service_account.created_by

    @staticmethod
    def resolve_org_roles(user: User) -> list[str]:
        return org_role_names(user)


class ServiceAccountIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=1000)


class ServiceAccountPatch(Schema):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=1000)
    active: bool | None = None


def org_role_names(user: User) -> list[str]:
    return sorted(str(r) for r in user.org_roles.values_list("role", flat=True))


def org_roles_from(names: list[OrgRoleName]) -> set[OrgRole]:
    return {OrgRole(name) for name in names}
