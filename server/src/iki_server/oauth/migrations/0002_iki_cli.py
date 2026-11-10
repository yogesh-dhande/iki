"""Iki's own command-line and SDK client: public, device flow only, approved."""

from typing import Any

from django.db import migrations

CLIENT_ID = "iki-cli"


def create(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401
    Client = apps.get_model("iki_oauth", "Client")
    Client.objects.get_or_create(
        client_id=CLIENT_ID,
        defaults={
            "name": "Iki CLI and Python SDK",
            "client_type": "public",
            "authorization_grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_secret": "",
            "hash_client_secret": False,
            "redirect_uris": "",
            "approval": "approved",
            "first_party": True,
            "registration_source": "manual",
        },
    )


def remove(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401
    apps.get_model("iki_oauth", "Client").objects.filter(client_id=CLIENT_ID).delete()


class Migration(migrations.Migration):
    dependencies = [("iki_oauth", "0001_initial")]
    operations = [migrations.RunPython(create, remove)]
