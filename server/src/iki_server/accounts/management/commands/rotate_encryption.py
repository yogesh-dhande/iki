"""``iki-server rotate_encryption``: re-encrypts stored secrets with the primary key.

Key rotation: put a new key first in ``IKI_ENCRYPTION_KEYS`` (keeping the old
one after it), deploy, run this command, then remove the old key. It re-encrypts
every value :data:`iki_server.core.crypto.ENCRYPTED` lists: identity providers'
client secrets and webhooks' signing secrets.
"""

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from iki_server.audit import log as audit
from iki_server.core import crypto


class Command(BaseCommand):
    help = "Re-encrypt stored secrets with the first key in IKI_ENCRYPTION_KEYS."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        count = 0
        with audit.cli(), transaction.atomic():
            for model, field in crypto.encrypted_fields():
                rows = model._default_manager.select_for_update().exclude(**{field: ""})
                for pk, token in rows.values_list("pk", field):
                    # An update, not a save: the value is the same, so nothing else changes.
                    model._default_manager.filter(pk=pk).update(**{field: crypto.rotate(token)})
                    count += 1
            audit.record("encryption.rotated", data={"secrets": count})
        self.stdout.write(f"Re-encrypted {count} secret(s) with the primary key.")
