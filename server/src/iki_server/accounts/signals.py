"""Signal receivers for accounts."""

from typing import Any

import structlog
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save

from iki_server.accounts import session_policy
from iki_server.accounts.models import User, UserSession

logger = structlog.get_logger("iki.accounts")


def end_sessions_of_inactive_users(instance: User, **_: Any) -> None:  # noqa: ANN401
    """Deactivating a user ends every session they have, at once."""
    if not instance.is_active:
        ended, _ = UserSession.objects.filter(user=instance).delete()
        if ended:
            logger.info("sessions_revoked", user_id=str(instance.pk), reason="deactivated")


def log_sign_in(user: User, **_: Any) -> None:  # noqa: ANN401
    logger.info("signed_in", user_id=str(user.pk))


def log_sign_out(user: User | None, **_: Any) -> None:  # noqa: ANN401
    if user is not None:
        logger.info("signed_out", user_id=str(user.pk))


def connect() -> None:
    post_save.connect(end_sessions_of_inactive_users, sender=User, dispatch_uid="iki-end-sessions")
    user_logged_in.connect(session_policy.start, dispatch_uid="iki-session-start")
    user_logged_in.connect(log_sign_in, dispatch_uid="iki-log-sign-in")
    user_logged_out.connect(log_sign_out, dispatch_uid="iki-log-sign-out")
