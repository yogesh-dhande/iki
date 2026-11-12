"""Iki's worker, signed in on someone's own computer (ADR 0058): public, device flow
only, approved, and limited to the ``worker`` scope, which no other client may ask for."""

from typing import Any

from django.db import migrations

CLIENT_ID = "iki-worker"


def create(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401
    Client = apps.get_model("iki_oauth", "Client")
    Client.objects.get_or_create(
        client_id=CLIENT_ID,
        defaults={
            "name": "Iki worker",
            "client_type": "public",
            "authorization_grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_secret": "",
            "hash_client_secret": False,
            "redirect_uris": "",
            "approval": "approved",
            "first_party": True,
            "registration_source": "manual",
            "allowed_scopes": "worker",
        },
    )


def remove(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401
    apps.get_model("iki_oauth", "Client").objects.filter(client_id=CLIENT_ID).delete()


class Migration(migrations.Migration):
    dependencies = [("iki_oauth", "0002_iki_cli")]
    operations = [migrations.RunPython(create, remove)]
