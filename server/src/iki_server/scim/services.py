"""What SCIM requests do: provision people, apply group changes, keep linked teams in sync.

Every change records an audit event in its transaction, through the SCIM
channel, naming the token that made it. The HTTP layer
(:mod:`iki_server.scim.views`) only translates SCIM documents into calls here.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org.models import Team, TeamMembership
from iki_server.scim.models import ScimGroup, ScimGroupMember

#: How many names an audit event lists before only counting the rest.
LISTED = 20


class ScimError(Exception):
    """A request SCIM clients must be told about (RFC 7644 §3.12)."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def conflict(detail: str) -> ScimError:
    return ScimError(409, detail, "uniqueness")


def invalid(detail: str) -> ScimError:
    return ScimError(400, detail, "invalidValue")


# --- People ----------------------------------------------------------------------


@dataclass
class Profile:
    """A person as SCIM describes them. ``None`` means "not given"."""

    user_name: str | None = None
    external_id: str | None = None
    email: str | None = None
    name: dict[str, str] = field(default_factory=dict)
    display_name: str | None = None
    active: bool | None = None


def _full_name(name: dict[str, str]) -> str:
    """The display name, else the name parts, else the formatted name."""
    composed = " ".join(
        part
        for part in (name.get("givenName"), name.get("middleName"), name.get("familyName"))
        if part
    )
    return (name.get("displayName") or composed or name.get("formatted") or "")[:200]


def _names(profile: Profile) -> dict[str, str]:
    names = dict(profile.name)
    if profile.display_name is not None:
        names["displayName"] = profile.display_name
    return names


def _email(profile: Profile, fallback: str = "") -> str:
    email = profile.email or ""
    if not email and profile.user_name and "@" in profile.user_name:
        email = profile.user_name
    email = User.objects.normalize_email(email.strip()) if email else fallback
    try:
        validate_email(email)
    except ValidationError:
        raise invalid("A person needs an email address, in emails or userName.") from None
    return email


def _set_active(user: User, active: bool) -> None:
    if user.is_active == active:
        return
    user.is_active = active
    # Saving an inactive user ends their sessions (accounts.signals).
    user.save(update_fields=["is_active"])
    audit.record("user.activated" if active else "user.deactivated", target=user)


def provision(profile: Profile) -> User:
    """Creates a person, or takes over the matching account Iki already has.

    Someone who signed in before SCIM was turned on keeps their account,
    roles and history; it becomes managed by the identity provider.
    """
    if not profile.user_name:
        raise invalid("userName is required.")
    email = _email(profile)
    with transaction.atomic():
        matches = list(
            User.objects.select_for_update().filter(
                Q(email=email) | Q(scim_user_name=profile.user_name)
            )
        )
        if len(matches) > 1 or any(
            user.is_provisioned or user.is_service_account for user in matches
        ):
            raise conflict(f"A person with userName or email {email} already exists.")
        adopted = bool(matches)
        user = matches[0] if adopted else User(email=email)
        if not adopted:
            user.set_unusable_password()
        user.email = email
        user.scim_user_name = profile.user_name
        user.scim_external_id = profile.external_id or ""
        user.scim_name = _names(profile)
        user.name = _full_name(user.scim_name) or user.name
        user.provisioned_at = timezone.now()
        was_active = user.is_active
        user.is_active = True if not adopted else was_active
        user.save()
        audit.record(
            "user.provisioned",
            target=user,
            data={"adopted": adopted, "external_id": user.scim_external_id},
        )
        if profile.active is not None:
            _set_active(user, profile.active)
    return user


_PROFILE_FIELDS = ("email", "name", "scim_user_name", "scim_external_id")


def update(user: User, profile: Profile) -> User:
    """Applies the given attributes; ``None`` leaves an attribute unchanged."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        before = {f: getattr(user, f) for f in _PROFILE_FIELDS}
        if profile.user_name is not None:
            if not profile.user_name:
                raise invalid("userName can't be empty.")
            user.scim_user_name = profile.user_name
        if profile.external_id is not None:
            user.scim_external_id = profile.external_id
        if profile.email is not None:
            user.email = _email(profile, fallback=user.email)
        names = _names(profile)
        if names:
            user.scim_name = {**user.scim_name, **names}
            user.name = _full_name(user.scim_name) or user.name
        changes = {
            f: [before[f], getattr(user, f)]
            for f in _PROFILE_FIELDS
            if getattr(user, f) != before[f]
        }
        if changes or names:
            taken = User.objects.exclude(pk=user.pk).filter(
                Q(email=user.email) | Q(scim_user_name=user.scim_user_name)
            )
            if taken.exists():
                raise conflict("Another person already has that userName or email.")
            try:
                user.save(update_fields=[*_PROFILE_FIELDS, "scim_name"])
            except IntegrityError as error:
                raise conflict("Another person already has that userName or email.") from error
        if changes:
            audit.record("user.updated", target=user, data=changes)
        if profile.active is not None:
            _set_active(user, profile.active)
    return user


def deprovision(user: User) -> None:
    """SCIM DELETE: the person is deactivated and stops being managed by SCIM.

    The account itself stays, so the audit log and project history keep
    pointing at a real person. Provisioning them again takes the account
    back over.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        teams = _teams_of_groups(ScimGroupMember.objects.filter(user=user).values("group"))
        ScimGroupMember.objects.filter(user=user).delete()
        _set_active(user, False)
        user.provisioned_at = None
        user.scim_user_name = ""
        user.scim_external_id = ""
        user.save(update_fields=["provisioned_at", "scim_user_name", "scim_external_id"])
        audit.record("user.deprovisioned", target=user)
        for team in teams:
            sync_team(team)


# --- Groups ----------------------------------------------------------------------


def _members(ids: Iterable[UUID]) -> list[User]:
    """Provisioned people with these IDs. Unknown IDs are ignored, as IdPs expect."""
    return list(User.objects.filter(pk__in=list(ids), provisioned_at__isnull=False))


def _teams_of_groups(groups: Any) -> list[Team]:  # noqa: ANN401 - a queryset of group IDs
    return list(Team.objects.filter(scim_groups__in=groups).distinct())


def _people(users: Iterable[User]) -> list[str]:
    return sorted(u.email for u in users)[:LISTED]


def _apply_members(
    group: ScimGroup, *, add: Iterable[User] = (), remove: Iterable[User] = ()
) -> dict[str, Any]:
    current = set(group.memberships.values_list("user_id", flat=True))
    added = [u for u in {u.pk: u for u in add}.values() if u.pk not in current]
    removed = [u for u in {u.pk: u for u in remove}.values() if u.pk in current]
    ScimGroupMember.objects.bulk_create(ScimGroupMember(group=group, user=u) for u in added)
    group.memberships.filter(user__in=removed).delete()
    if not (added or removed):
        return {}
    return {
        "members_added": len(added),
        "members_removed": len(removed),
        "added": _people(added),
        "removed": _people(removed),
    }


def _locked(group: ScimGroup) -> ScimGroup:
    return ScimGroup.objects.select_for_update().get(pk=group.pk)


def _save_group(
    group: ScimGroup, display_name: str | None, external_id: str | None
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if display_name is not None:
        if not display_name.strip():
            raise invalid("displayName can't be empty.")
        if display_name != group.display_name:
            changes["display_name"] = [group.display_name, display_name]
            group.display_name = display_name
    if external_id is not None and external_id != group.external_id:
        changes["external_id"] = [group.external_id, external_id]
        group.external_id = external_id
    if changes:
        if ScimGroup.objects.exclude(pk=group.pk).filter(display_name=group.display_name).exists():
            raise conflict(f"A group called {group.display_name} already exists.")
        group.save()
    return changes


def _after_member_change(group: ScimGroup, data: dict[str, Any]) -> None:
    if data:
        audit.record("scim.group_updated", target=group, team=group.team, data=data)
    if group.team is not None and any(k.startswith("members") for k in data):
        sync_team(group.team)


def create_group(display_name: str, external_id: str, member_ids: Iterable[UUID]) -> ScimGroup:
    if not display_name.strip():
        raise invalid("displayName is required.")
    try:
        with transaction.atomic():
            group = ScimGroup.objects.create(display_name=display_name, external_id=external_id)
            members = _members(member_ids)
            ScimGroupMember.objects.bulk_create(
                ScimGroupMember(group=group, user=u) for u in members
            )
            audit.record(
                "scim.group_created",
                target=group,
                data={"members": len(members), "people": _people(members)},
            )
    except IntegrityError as error:
        raise conflict(f"A group called {display_name} already exists.") from error
    return group


def replace_group(
    group: ScimGroup, display_name: str, external_id: str | None, member_ids: Iterable[UUID]
) -> ScimGroup:
    with transaction.atomic():
        group = _locked(group)
        data = _save_group(group, display_name, external_id)
        wanted = _members(member_ids)
        current = set(group.memberships.values_list("user_id", flat=True))
        data |= _apply_members(
            group,
            add=wanted,
            remove=User.objects.filter(pk__in=current - {u.pk for u in wanted}),
        )
        _after_member_change(group, data)
    return group


@dataclass
class GroupPatch:
    display_name: str | None = None
    external_id: str | None = None
    add: list[UUID] = field(default_factory=list)
    remove: list[UUID] = field(default_factory=list)
    #: Replace the member list with exactly these (applied before add/remove).
    replace: list[UUID] | None = None
    remove_all: bool = False


def patch_group(group: ScimGroup, patch: GroupPatch) -> ScimGroup:
    with transaction.atomic():
        group = _locked(group)
        data = _save_group(group, patch.display_name, patch.external_id)
        current = set(group.memberships.values_list("user_id", flat=True))
        add = _members(patch.add)
        remove_ids = set(patch.remove)
        if patch.remove_all:
            remove_ids |= current
        if patch.replace is not None:
            wanted = _members(patch.replace)
            add += wanted
            remove_ids |= current - {u.pk for u in wanted}
        remove_ids -= {u.pk for u in add}
        data |= _apply_members(group, add=add, remove=User.objects.filter(pk__in=remove_ids))
        _after_member_change(group, data)
    return group


def delete_group(group: ScimGroup) -> None:
    """The group goes away. A team it was linked to keeps its members and is managed by hand."""
    with transaction.atomic():
        group = _locked(group)
        team = group.team
        audit.record(
            "scim.group_deleted",
            target=group,
            team=team,
            data={"display_name": group.display_name, "team": team.name if team else None},
        )
        group.delete()
        if team is not None:
            sync_team(team)


# --- Teams -----------------------------------------------------------------------


def sync_team(team: Team) -> None:
    """Makes a linked team's members exactly the members of its groups.

    A team no group is linked to is left alone.
    """
    with transaction.atomic():
        team = Team.objects.select_for_update().get(pk=team.pk)
        groups = list(ScimGroup.objects.filter(team=team).order_by("display_name"))
        if not groups:
            return
        names = [g.display_name for g in groups]
        wanted = set(
            ScimGroupMember.objects.filter(group__in=groups).values_list("user_id", flat=True)
        )
        current = set(TeamMembership.objects.filter(team=team).values_list("user_id", flat=True))
        joining = User.objects.filter(pk__in=wanted - current)
        leaving = User.objects.filter(pk__in=current - wanted)
        for user in joining:
            TeamMembership.objects.create(team=team, user=user, added_by=None)
            audit.record("team.member_added", target=user, team=team, data={"groups": names})
        for user in leaving:
            TeamMembership.objects.filter(team=team, user=user).delete()
            audit.record("team.member_removed", target=user, team=team, data={"groups": names})


@dataclass(frozen=True)
class LinkPreview:
    joining: list[User]
    leaving: list[User]


def preview_link(group: ScimGroup, team: Team) -> LinkPreview:
    """Who linking ``group`` to ``team`` would add and remove."""
    groups = [*ScimGroup.objects.filter(team=team).exclude(pk=group.pk), group]
    wanted = set(ScimGroupMember.objects.filter(group__in=groups).values_list("user_id", flat=True))
    current = set(TeamMembership.objects.filter(team=team).values_list("user_id", flat=True))
    return LinkPreview(
        joining=list(User.objects.filter(pk__in=wanted - current)),
        leaving=list(User.objects.filter(pk__in=current - wanted)),
    )


def link_group(group: ScimGroup, team: Team, *, by: User) -> None:
    """From now on, ``group`` decides who is on ``team`` (with any other linked groups)."""
    with transaction.atomic():
        group = _locked(group)
        if group.team_id == team.pk:
            return
        previous = group.team
        group.team = team
        group.save(update_fields=["team", "updated_at"])
        audit.record(
            "scim.group_linked",
            actor=by,
            target=group,
            team=team,
            data={"previous_team": previous.name if previous else None},
        )
        sync_team(team)
        if previous is not None:
            sync_team(previous)


def unlink_group(group: ScimGroup, *, by: User) -> None:
    """The team keeps its current members; if no other group is linked, it's managed by hand."""
    with transaction.atomic():
        group = _locked(group)
        team = group.team
        if team is None:
            return
        group.team = None
        group.save(update_fields=["team", "updated_at"])
        audit.record("scim.group_unlinked", actor=by, target=group, team=team)
        sync_team(team)
