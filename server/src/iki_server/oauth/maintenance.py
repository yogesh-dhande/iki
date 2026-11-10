"""Housekeeping for OAuth, run by ``iki-server maintain``."""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from oauth2_provider.models import clear_expired, get_device_grant_model, get_grant_model

from iki_server.oauth.models import Approval, Client

#: Self-registered apps nobody asked to approve are deleted after this long.
ABANDONED_AFTER = timedelta(days=7)


def prune() -> int:
    """Deletes expired tokens and grants, and abandoned registrations. Returns how many apps."""
    now = timezone.now()
    clear_expired()
    get_grant_model().objects.filter(expires__lt=now).delete()
    get_device_grant_model().objects.filter(expires__lt=now).delete()
    _, deleted = (
        Client.objects.filter(
            approval=Approval.PENDING,
            requested_by__isnull=True,
            created__lt=now - ABANDONED_AFTER,
        )
        .filter(Q(registration_source="dcr") | Q(registration_source="cimd"))
        .delete()
    )
    return int(deleted.get(Client._meta.label, 0))
