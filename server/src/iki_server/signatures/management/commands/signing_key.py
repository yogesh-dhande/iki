"""``iki-server signing_key``: this deployment's keys for electronic signatures (ADR 0126).

With no options it prints each key, oldest first, for whoever checks signatures offline
(``iki bundle verify --key``). ``--rotate`` retires the key that signs now for a new one,
as a security policy or a suspected compromise asks: what the old key signed still
verifies, and it signs nothing more. Rotating is in the audit log.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from iki_server.audit import log
from iki_server.signatures import services, signing_keys


class Command(BaseCommand):
    help = "Print this deployment's signature keys, or retire the current one for a new one."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--rotate",
            action="store_true",
            help="Retire the key that signs now, and make a new one.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        deployment = services.deployment()
        if options["rotate"]:
            before = [str(k["key"]) for k in signing_keys.own() if k["retired_at"] is None]
            key = signing_keys.rotate()
            log.record(
                "signature.key_rotated",
                data={"retired": before, "key": key.public, "via": "iki-server signing_key"},
            )
            self.stdout.write(f"{deployment}={key.public}")
            self.stderr.write(
                "Signatures are signed with that key from now on. "
                + (f"{', '.join(before)} is retired: " if before else "")
                + "what a retired key signed still verifies."
            )
            return
        found = signing_keys.own()
        if not found:
            self.stderr.write("No key yet: the first signature makes one.")
        for k in found:
            state = f"retired {k['retired_at']}" if k["retired_at"] else "signing now"
            self.stdout.write(f"{deployment}={k['key']}  made {k['created_at']}, {state}")
