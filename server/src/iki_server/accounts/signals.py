"""Signal receivers for accounts."""

from typing import Any

from allauth.account.signals import user_signed_up
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save

from iki_server.accounts import session_policy
from iki_server.accounts.models import User, UserSession
from iki_server.audit import log as audit


def end_sessions_of_inactive_users(instance: User, **_: Any) -> None:  # noqa: ANN401
    """Deactivating a user (or service account) ends every session and OAuth token
    they have, at once, however they were deactivated."""
    if not instance.is_active:
        from iki_server.oauth.services import revoke_user_tokens

        ended, _ = UserSession.objects.filter(user=instance).delete()
        tokens = revoke_user_tokens(instance)
        if ended or tokens:
            audit.record(
                "user.sessions_revoked",
                target=instance,
                data={"sessions": ended, "tokens_revoked": tokens, "reason": "deactivated"},
            )


def record_sign_in(user: User, **_: Any) -> None:  # noqa: ANN401
    audit.record("user.signed_in", actor=user, target=user)


def record_sign_out(user: User | None, **_: Any) -> None:  # noqa: ANN401
    if user is not None:
        audit.record("user.signed_out", actor=user, target=user)


def record_first_sign_in(user: User, **_: Any) -> None:  # noqa: ANN401
    audit.record("user.created", actor=user, target=user, data={"name": user.name})


def connect() -> None:
    post_save.connect(end_sessions_of_inactive_users, sender=User, dispatch_uid="iki-end-sessions")
    user_logged_in.connect(session_policy.start, dispatch_uid="iki-session-start")
    user_logged_in.connect(record_sign_in, dispatch_uid="iki-audit-sign-in")
    user_logged_out.connect(record_sign_out, dispatch_uid="iki-audit-sign-out")
    user_signed_up.connect(record_first_sign_in, dispatch_uid="iki-audit-first-sign-in")
