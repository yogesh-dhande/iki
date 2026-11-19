"""What a client's token may do now, as its person's choice stands (ADR 0028, 0152).

Every check of a token starts here, so each one sees the same thing: a request to the API,
an open event stream at each event, and a webhook the client added before each message.
Changing or revoking a consent applies to all of them at once.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from iki_server.oauth.models import Client, Consent
from iki_server.org import permissions


@dataclass(frozen=True)
class Access:
    #: Who the token acts as: its person, or a credential's service account.
    user: Any
    limits: permissions.TokenLimits
    #: The person's consent, for a client people sign in to or delegate to.
    consent: Consent | None
    #: The consent, when it lets a service account's credential act for the person
    #: (ADR 0073).
    delegation: Consent | None


def of(client: Client, person: Any, scopes: Iterable[str]) -> Access | None:  # noqa: ANN401
    """What a token `client` holds for `person` (None for a service account's own) may do,
    asked with `scopes`. None when it may do nothing: the person revoked their consent, it
    expired, or the person or the service account was deactivated."""
    user = person or client.service_account
    if user is None or not user.is_active:
        return None
    granted = frozenset(scopes)
    projects = None
    only_projects = False
    consent = None
    # A person's token from a service account's credential: one it got by acting for
    # them, under their delegation (ADR 0073).
    account = client.service_account if person is not None else None
    if client.service_account_id is None or account is not None:
        consent = (
            Consent.objects.filter(user=user, client=client).prefetch_related("projects").first()
        )
        if consent is None or consent.expired:
            return None
        # A narrowed consent applies to tokens issued before it changed.
        granted &= frozenset(consent.scope_list)
        if not consent.all_projects:
            projects = frozenset(p.pk for p in consent.projects.all())
            only_projects = True
    if account is not None:
        if not (client.acts_for_people and account.is_active):
            return None
        # Only where the service account may go too.
        theirs = frozenset(permissions.visible_projects(account).values_list("pk", flat=True))
        projects = theirs if projects is None else projects & theirs
    limits = permissions.TokenLimits(
        scopes=granted, projects=projects, alongside=account, only_projects=only_projects
    )
    return Access(user, limits, consent, consent if account is not None else None)


def under(consent: Consent) -> Any | None:  # noqa: ANN401
    """The person, as their consent lets its client act for them now, for deciding what
    the client may still do outside any request; None when it may do nothing."""
    from iki_server.accounts.models import User

    client = consent.client
    if not client.is_approved:
        return None
    # A user object of its own: its decisions are cached on it, and the limits applied.
    person = User.objects.filter(pk=consent.user_id).first()
    if person is None:
        return None
    found = of(client, person, consent.scope_list)
    if found is None or found.consent is None or found.consent.grant != consent.grant:
        return None
    permissions.limit_to(found.user, found.limits)
    return found.user
