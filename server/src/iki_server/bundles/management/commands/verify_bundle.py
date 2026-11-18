"""``iki-server verify_bundle FILE``: checks a bundle's bytes and signature, and says what's in it.

It checks what an import would check first (ADR 0096): that every member is listed in
``bundle.json`` with its SHA-256, that the signature is of ``bundle.json`` by the key it
names, and whether this deployment trusts that key. It changes nothing. It exits 1 if the
bundle can't be read, has been changed, or isn't from a deployment this one trusts.
"""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from iki_server.bundles import reading


class Command(BaseCommand):
    help = "Check a bundle's members and signature, and say what's in it. It changes nothing."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("file", type=Path)

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        path: Path = options["file"]
        if not path.is_file():
            raise CommandError(f"{path} isn't a file.")
        try:
            bundle = reading.read(
                path, settings.IKI_BUNDLE_TRUSTED_KEYS, limit=settings.IKI_BUNDLE_MAX_BYTES
            )
        except reading.BundleError as error:
            raise CommandError(f"{path} {_lower(str(error))}") from None
        m = bundle.manifest
        counts = m["counts"]
        by = m["exported_by"]
        self.stdout.write(
            f"{path}: bundle {m['id']}, SHA-256 {bundle.sha256}.\n"
            f"  From {m['project']['name']} ({m['project']['team']}) on {bundle.source}, "
            f"Iki {m['source']['iki']}: {m['selection']}\n"
            f"  Exported by {by['name']} <{by['email']}> at {m['created_at']}: {m['reason']}\n"
            f"  {counts['entities']} entities, {counts['revisions']} revisions, "
            f"{counts['files']} files; {counts['outside']} referred to but not in it.\n"
            f"  Every member matches bundle.json, and bundle.json is signed by "
            f"{m['source']['key']}."
        )
        if bundle.trusted_as is None:
            raise CommandError(
                f"This deployment doesn't trust that key for {bundle.source}, so it wouldn't "
                f"import it. If {bundle.source} is meant to send it bundles, add "
                f"{bundle.source}={m['source']['key']} to IKI_BUNDLE_TRUSTED_KEYS (check the key "
                f"with `iki-server bundle_key --public` on {bundle.source})."
            )
        self.stdout.write(f"  This deployment trusts it as {bundle.trusted_as}.")
        from iki_server.signatures import offline

        verified = offline.verify(bundle)
        for v in verified:
            mark = "ok" if v.valid else "FAILED"
            self.stdout.write(f"  Signature {mark}: {v.hrid}: {v.manifestation} ({v.status})")
            for check in v.checks:
                if not check.ok:
                    self.stdout.write(f"    {check.name}: {check.detail}")
        if failed := sum(1 for v in verified if not v.valid):
            raise CommandError(f"{failed} of its {len(verified)} signatures don't verify.")


def _lower(message: str) -> str:
    """ "It has no bundle.json" → "has no bundle.json", after the path."""
    return message[3:] if message.startswith("It ") else f": {message}"
