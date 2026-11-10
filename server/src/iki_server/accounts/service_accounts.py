"""Creating and changing service accounts (ADR 0027).

Callers check that the person acting may manage the organization
(:attr:`~iki_server.org.permissions.Action.ORG_MANAGE`). Deactivating and
reactivating go through :func:`iki_server.org.services.set_active`, as for
people; org roles through :func:`iki_server.org.services.set_org_roles`.
"""

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from iki_server.accounts.models import SERVICE_ACCOUNT_DOMAIN, Kind, ServiceAccount, User
from iki_server.audit import log as audit
from iki_server.org.services import Refused

NAME_MAX_LENGTH = 100


def _address(name: str) -> str:
    """A unique address from the name, kept when the account is renamed."""
    base = slugify(name)[:50].strip("-") or "service-account"
    address, n = f"{base}@{SERVICE_ACCOUNT_DOMAIN}", 2
    while User.objects.filter(email=address).exists():
        address, n = f"{base}-{n}@{SERVICE_ACCOUNT_DOMAIN}", n + 1
    return address


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise Refused("Give the service account a name.")
    if len(name) > NAME_MAX_LENGTH:
        raise Refused(f"Keep the name under {NAME_MAX_LENGTH} characters.")
    return name


def _taken(name: str) -> Refused:
    return Refused(f"There's already a service account called “{name}”.")


def create(name: str, description: str, *, by: User) -> User:
    name = _clean_name(name)
    try:
        with transaction.atomic():
            user = User(kind=Kind.SERVICE, email=_address(name), name=name)
            user.set_unusable_password()
            user.save()
            ServiceAccount.objects.create(user=user, description=description, created_by=by)
            audit.record("service_account.created", actor=by, target=user, data={"name": name})
    except IntegrityError as error:
        raise _taken(name) from error
    return user


def update(user: User, *, name: str, description: str, by: User) -> None:
    if not user.is_service_account:
        raise TypeError("not a service account")
    name = _clean_name(name)
    details = user.service_account
    changes = {
        field: [old, new]
        for field, old, new in (
            ("name", user.name, name),
            ("description", details.description, description),
        )
        if old != new
    }
    if not changes:
        return
    try:
        with transaction.atomic():
            user.name = name
            user.save(update_fields=["name"])
            details.description = description
            details.save(update_fields=["description"])
            audit.record("service_account.updated", actor=by, target=user, data=changes)
    except IntegrityError as error:
        raise _taken(name) from error
