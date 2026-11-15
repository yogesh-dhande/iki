"""``iki-server bundle_key``: makes a bundle signing key, or prints this deployment's.

Bundles carry entities between deployments, signed by the one that made them (ADR 0096).
With no options it makes a new key and prints only it on standard output, for the
deployment's secret (``IKI_BUNDLE_SIGNING_KEY``)::

    iki-server bundle_key > key
    kubectl create secret generic iki-bundle-key -n iki --from-file=key

The public key goes to standard error. ``--public`` prints this deployment's public key,
for the deployments that import its bundles to list as trusted.
"""

import sys
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from iki_server.bundles import keys


class Command(BaseCommand):
    help = "Make a bundle signing key (printed alone on standard output), or print the public key."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--public",
            action="store_true",
            help="Print this deployment's public key, for others to trust its bundles.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        if options["public"]:
            if not settings.IKI_BUNDLE_SIGNING_KEY:
                raise CommandError(
                    "This deployment has no bundle signing key (IKI_BUNDLE_SIGNING_KEY)."
                )
            public = keys.public_text(keys.private_key(settings.IKI_BUNDLE_SIGNING_KEY))
            self.stdout.write(f"{settings.IKI_ENVIRONMENT}={public}")
            return
        private, public = keys.generate()
        self.stdout.write(private)
        sys.stderr.write(
            f"Its public key is {public}.\n"
            "Store the key above in this deployment's secret (IKI_BUNDLE_SIGNING_KEY). "
            "Deployments that import its bundles list the public key as trusted "
            f"(IKI_BUNDLE_TRUSTED_KEYS: {settings.IKI_ENVIRONMENT}={public}).\n"
        )
