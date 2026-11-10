"""What the API sends and accepts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from ninja import Field, Schema

from iki_server.accounts.models import User
from iki_server.org.models import OrgRole, ProjectMembership, ProjectRole, Team

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


class MeOut(UserOut):
    org_roles: list[OrgRoleName]


class OrgRolesIn(Schema):
    roles: list[OrgRoleName]


class OrgRolesOut(Schema):
    roles: list[OrgRoleName]


class UserPatch(Schema):
    active: bool | None = None


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


class ProjectOut(Schema):
    id: UUID
    name: str
    slug: str
    description: str
    team: TeamRef
    archived: bool = Field(alias="is_archived")
    created_at: datetime
    my_role: ProjectRoleName | None = Field(
        description="Your role, from your own grant or your teams'; null if you have none."
    )

    @staticmethod
    def resolve_my_role(project: object) -> ProjectRoleName | None:
        return role_name(getattr(project, "my_role", None))


class ProjectIn(Schema):
    team: UUID
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=2000)


class ProjectPatch(Schema):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    archived: bool | None = None


class ProjectMemberOut(Schema):
    id: UUID
    role: ProjectRoleName
    user: UserOut | None
    team: TeamRef | None
    added_at: datetime

    @staticmethod
    def resolve_role(membership: ProjectMembership) -> ProjectRoleName | None:
        return role_name(membership.role)


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
