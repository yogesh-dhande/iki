"""``iki-server setup_link``: prints a one-time link that opens the setup wizard.

Run it inside the cluster, for example::

    kubectl exec -n iki deploy/iki-web -- iki-server setup_link

Being able to run it is the proof of authority: there is no default
password. The link works once, for 60 minutes, and creating a new link
revokes earlier ones. ``--recover`` reconfigures sign-in on an installation
that is already set up (for example after the identity provider changed).
"""

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.urls import reverse

from iki_server.audit import log as audit
from iki_server.setup.models import LINK_LIFETIME, Installation, SetupToken


def _base_url(explicit: str | None) -> str:
    url = (explicit or settings.IKI_PUBLIC_URL or "").rstrip("/")
    if not url:
        raise CommandError("Pass --url with the address users open Iki at, or set IKI_PUBLIC_URL.")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.path not in ("", "/"):
        raise CommandError(f"{url!r} is not a base URL like https://iki.example.com")
    return url


class Command(BaseCommand):
    help = "Print a one-time link to the setup wizard."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--recover",
            action="store_true",
            help="Reconfigure sign-in on an installation that is already set up.",
        )
        parser.add_argument("--url", help="Base URL of Iki (defaults to IKI_PUBLIC_URL).")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        installation = Installation.get()
        recover: bool = options["recover"]
        if installation.is_set_up and not recover:
            raise CommandError(
                "Iki is already set up. To reconfigure sign-in (break-glass), "
                "run `iki-server setup_link --recover`."
            )
        base = _base_url(options.get("url"))
        with audit.cli(), transaction.atomic():
            record, token = SetupToken.issue(recovery=recover and installation.is_set_up)
            audit.record(
                "setup.link_created",
                data={"recovery": record.recovery, "expires_at": record.expires_at.isoformat()},
            )
        minutes = int(LINK_LIFETIME.total_seconds() // 60)
        # The token is in the fragment, so it never reaches server logs or Referer headers.
        link = f"{base}{reverse('setup:start')}#{token}"
        self.stdout.write(
            f"\nSetup link (works once, for {minutes} minutes):\n\n  {link}\n\n"
            "Anyone with this link can configure sign-in for Iki. "
            "Open it yourself and don't share it.\n"
        )
