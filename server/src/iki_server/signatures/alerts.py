"""Telling the organization's security people, at once, of attempts to sign as someone
(21 CFR 11.300(d)): each attempt that might be someone else's, and the lock that follows
too many of them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from iki_server.audit import log
from iki_server.notifications import kinds
from iki_server.notifications.services import notify, org_admins

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.entities.models import Entity

ATTEMPT = kinds.register(
    kinds.Kind(
        "signature.attempt_refused",
        "An attempt to sign was refused",
        "Signing in again to sign a record didn't prove it was its signer: someone else "
        "signed in at the identity provider, or its answer didn't check out.",
        group="Security",
        audience=kinds.Audience.ORG_ADMINS,
    )
)
LOCKED = kinds.register(
    kinds.Kind(
        "signature.locked",
        "Someone's signing was locked",
        "Failed attempts to sign as someone locked their signing, and ended their sessions, "
        "until an Org Admin unlocks it. They're told too.",
        group="Security",
    )
)


def attempt(user: User, entity: Entity, why: str) -> None:
    """Tells Org Admins of one attempt that might be someone else's."""
    who = user.name or user.email
    notify(
        org_admins(),
        ATTEMPT,
        title=f"An attempt to sign {entity.hrid} as {who} was refused",
        body=why,
        path=f"/entities/{entity.hrid}/",
    )


def lock(user: User, failures: int) -> None:
    """Locks the person's signing and ends their sessions, then tells them and Org Admins."""
    from iki_server.accounts.models import UserSession
    from iki_server.oauth.services import revoke_user_tokens

    log.record("signature.locked", target=user, data={"failures": failures})
    ended, _ = UserSession.objects.filter(user=user).delete()
    tokens = revoke_user_tokens(user)
    log.record(
        "user.sessions_revoked",
        target=user,
        data={"sessions": ended, "tokens_revoked": tokens, "reason": "signing_locked"},
    )
    who = user.name or user.email
    body = (
        f"{failures} attempts to sign as {who} failed within minutes, so their signing is "
        "locked and their sessions ended. An Org Admin unlocks it in Settings, Signatures, "
        "once they know who it was."
    )
    notify(
        org_admins(),
        LOCKED,
        title=f"{who}'s signing is locked",
        body=body,
        path="/settings/signatures/",
    )
    notify(
        [user],
        LOCKED,
        title="Your signing is locked",
        body=(
            f"{failures} attempts to sign as you failed, so your signing is locked and you were "
            "signed out. If they weren't yours, tell your security team. An Org Admin unlocks "
            "it."
        ),
    )
