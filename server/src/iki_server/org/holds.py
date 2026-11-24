"""Legal holds (ADR 0092): a project kept whole for a legal or regulatory matter.

While a project has a hold on, nothing in it is deleted. Run artifacts stay past their
retention, and a plugin's purge refuses to start. Everything else carries on as usual:
people still work in it, and archiving still works, since an archived project keeps
everything. Org Admins place and release holds; Auditors can read them.
"""

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org import permissions
from iki_server.org.models import LegalHold, Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

MAX_MATTER = 200
MAX_RELEASE_REASON = 2000


def on() -> QuerySet[LegalHold]:
    """The holds in force."""
    return LegalHold.objects.filter(released_at__isnull=True)


def held_projects() -> QuerySet[Project]:
    """Projects with a hold on: nothing in them is deleted."""
    return Project.objects.filter(pk__in=on().values("project"))


def is_held(project: Project) -> bool:
    return on().filter(project=project).exists()


def place(by: User, project: Project, matter: str, note: str = "") -> LegalHold:
    """Puts the project on hold for ``matter``. Raises :class:`Refused` if it already is."""
    permissions.require(by, Action.ORG_MANAGE)
    matter = matter.strip()
    if not matter:
        raise Refused("Say what the hold is for, such as a case or an inspection.", field="matter")
    if len(matter) > MAX_MATTER:
        raise Refused(f"Keep it to {MAX_MATTER} characters.", field="matter")
    try:
        with transaction.atomic():
            hold = LegalHold.objects.create(
                project=project, matter=matter, note=note.strip(), placed_by=by
            )
            audit.record(
                "legal_hold.placed",
                actor=by,
                target=hold,
                project=project,
                data={"matter": matter},
            )
    except IntegrityError:
        raise Refused(f"{project.name} is already on hold for {matter}.", field="matter") from None
    return hold


def release(by: User, hold: LegalHold, *, reason: str) -> LegalHold:
    """Releases the hold with a reason. Its project may be deleted when no other hold is on."""
    permissions.require(by, Action.ORG_MANAGE)
    reason = reason.strip()
    if not reason:
        raise Refused("Say why the legal hold is being released.", field="reason")
    if len(reason) > MAX_RELEASE_REASON:
        raise Refused(
            f"Keep the release reason to {MAX_RELEASE_REASON} characters.", field="reason"
        )
    with transaction.atomic():
        hold = (
            LegalHold.objects.select_for_update(of=("self",))
            .select_related("project")
            .get(pk=hold.pk)
        )
        if hold.released_at is not None:
            return hold
        hold.released_at = timezone.now()
        hold.released_by = by
        hold.release_reason = reason
        hold.save(update_fields=["released_at", "released_by", "release_reason"])
        audit.record(
            "legal_hold.released",
            actor=by,
            target=hold,
            project=hold.project,
            data={"matter": hold.matter, "reason": reason},
        )
    return hold
