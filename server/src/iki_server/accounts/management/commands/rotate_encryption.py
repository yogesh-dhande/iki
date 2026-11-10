"""``iki-server rotate_encryption``: re-encrypts stored secrets with the primary key.

Key rotation: put a new key first in ``IKI_ENCRYPTION_KEYS`` (keeping the old
one after it), deploy, run this command, then remove the old key.
"""

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from iki_server.accounts.models import IdentityProvider
from iki_server.audit import log as audit
from iki_server.core import crypto


class Command(BaseCommand):
    help = "Re-encrypt stored secrets with the first key in IKI_ENCRYPTION_KEYS."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        count = 0
        with audit.cli(), transaction.atomic():
            for provider in IdentityProvider.objects.select_for_update():
                provider.client_secret_encrypted = crypto.rotate(provider.client_secret_encrypted)
                provider.save(update_fields=["client_secret_encrypted", "updated_at"])
                count += 1
            audit.record("encryption.rotated", data={"secrets": count})
        self.stdout.write(f"Re-encrypted {count} secret(s) with the primary key.")
