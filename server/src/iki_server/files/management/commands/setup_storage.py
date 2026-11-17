"""``iki-server setup_storage``: checks the file bucket, and gives it the rules Iki needs.

- **CORS:** browsers send upload parts straight to the bucket, and some pages
  (the structure viewer) read files from it, from Iki's public URL (and the web
  app's dev server, in development). The rule allows ``PUT`` and ``GET`` from
  those origins only, with links Iki signed.
- **Lifecycle:** multipart uploads abandoned for 2 days are aborted, and
  staging objects older than 2 days are deleted, in case maintenance never ran.
- **Versioning** is checked, not changed: turning it on is the bucket
  owner's decision, and Iki says so if it's off. ``--enable-versioning``
  turns it on (the dev cluster does).

Platform teams that manage the bucket themselves (OpenTofu, for example) can
run it with ``--check`` to see what's missing without changing anything.

On Cloud Storage (``IKI_FILES_BACKEND=gcs``) it checks and sets the same, through
Cloud Storage's own API, and checks too that the bucket encrypts with a
customer-managed key of its own, which, like versioning, it never changes.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from iki_server.files import storage

RULE_ID = "iki-uploads"
#: Named for uploads, which it allowed first; kept, so Iki recognizes its own rule.
CORS_ID = "iki-browser-uploads"
STALE_DAYS = 2


def origins() -> list[str]:
    found = [settings.IKI_PUBLIC_URL.rstrip("/")] if settings.IKI_PUBLIC_URL else []
    if settings.IKI_WEB_DEV_SERVER:
        found.append("http://localhost:5173")
    return found


def cors_rule() -> dict[str, Any]:
    return {
        "ID": CORS_ID,
        "AllowedOrigins": origins(),
        "AllowedMethods": ["GET", "PUT"],
        # Iki lists the parts itself, so pages needn't read any response header.
        "AllowedHeaders": ["content-type"],
        "MaxAgeSeconds": 3600,
    }


def lifecycle_rule() -> dict[str, Any]:
    prefix = storage.staging_key("").rstrip("/") + "/"
    return {
        "ID": RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": prefix},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": STALE_DAYS},
        "Expiration": {"Days": STALE_DAYS},
    }


class Command(BaseCommand):
    help = "Check the file bucket and give it the CORS and lifecycle rules Iki needs."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--check", action="store_true", help="Only report what's missing; change nothing."
        )
        parser.add_argument(
            "--enable-versioning", action="store_true", help="Turn on the bucket's versioning."
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        if not storage.configured():
            raise CommandError(str(storage.NotConfigured()))
        if not origins():
            raise CommandError("Set IKI_PUBLIC_URL, so browsers' uploads can be allowed from it.")
        if storage.scheme() == "gs":
            self._cloud_storage(check=options["check"], enable=options["enable_versioning"])
            return
        s3 = storage.client()
        bucket = storage.bucket()
        missing: list[str] = []

        versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if versioning != "Enabled" and options["enable_versioning"] and not options["check"]:
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
            versioning = "Enabled"
            self.stdout.write(f"Turned on versioning for bucket {bucket}.")
        if versioning != "Enabled":
            missing.append("versioning (turn it on, so stored files keep every version)")

        cors = _get(lambda: s3.get_bucket_cors(Bucket=bucket)["CORSRules"])
        others = [r for r in cors if r.get("ID") != CORS_ID]
        mine = next((r for r in cors if r.get("ID") == CORS_ID), None)
        if mine is None or not _same(mine, cors_rule()):
            missing.append("CORS for browsers' uploads and reads")
            if not options["check"]:
                s3.put_bucket_cors(
                    Bucket=bucket,
                    CORSConfiguration={"CORSRules": [*others, cors_rule()]},  # type: ignore[list-item]
                )

        rules = _get(lambda: s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"])
        others = [r for r in rules if r.get("ID") != RULE_ID]
        mine = next((r for r in rules if r.get("ID") == RULE_ID), None)
        if mine is None or not _same(mine, lifecycle_rule()):
            missing.append("lifecycle rules for abandoned uploads")
            if not options["check"]:
                s3.put_bucket_lifecycle_configuration(
                    Bucket=bucket,
                    LifecycleConfiguration={"Rules": [*others, lifecycle_rule()]},  # type: ignore[list-item]
                )

        if not missing:
            self.stdout.write(f"Bucket {bucket} is ready for Iki.")
            return
        if options["check"]:
            raise CommandError(f"Bucket {bucket} is missing: {'; '.join(missing)}.")
        fixed = [m for m in missing if not m.startswith("versioning")]
        if fixed:
            self.stdout.write(f"Set up on bucket {bucket}: {', '.join(fixed)}.")
        if versioning != "Enabled":
            self.stderr.write(
                f"Bucket {bucket} doesn't keep versions. Turn versioning on: Iki doesn't change "
                "a bucket's versioning itself."
            )

    def _cloud_storage(self, *, check: bool, enable: bool) -> None:
        from iki_server.files import gcs

        name = storage.bucket()
        bucket = gcs.clients().client.get_bucket(name)
        missing: list[str] = []
        changed = False
        if not bucket.versioning_enabled and enable and not check:
            bucket.versioning_enabled = changed = True
            self.stdout.write(f"Turned on versioning for bucket {name}.")
        if not bucket.versioning_enabled:
            missing.append("versioning (turn it on, so stored files keep every version)")
        if not bucket.default_kms_key_name:
            missing.append("a customer-managed key (give the bucket a default Cloud KMS key)")
        cors = [dict(rule) for rule in bucket.cors]
        if not any(_same(rule, gcs_cors_rule()) for rule in cors):
            missing.append("CORS for browsers' uploads and reads")
            bucket.cors = [*cors, gcs_cors_rule()]
            changed = True
        rules = [dict(rule) for rule in bucket.lifecycle_rules]
        absent = [want for want in gcs_lifecycle_rules() if not any(_same(r, want) for r in rules)]
        if absent:
            missing.append("lifecycle rules for abandoned uploads")
            bucket.lifecycle_rules = [*rules, *absent]
            changed = True
        fixable = [m for m in missing if not m.startswith(("versioning", "a customer-managed"))]
        if check and missing:
            raise CommandError(f"Bucket {name} is missing: {'; '.join(missing)}.")
        if changed:
            bucket.patch()
        if not missing:
            self.stdout.write(f"Bucket {name} is ready for Iki.")
            return
        if fixable:
            self.stdout.write(f"Set up on bucket {name}: {', '.join(fixable)}.")
        if not bucket.versioning_enabled:
            self.stderr.write(
                f"Bucket {name} doesn't keep versions. Turn versioning on: Iki doesn't change "
                "a bucket's versioning itself."
            )
        if not bucket.default_kms_key_name:
            self.stderr.write(
                f"Bucket {name} has no customer-managed key. Give it a default Cloud KMS key: "
                "Iki doesn't change a bucket's encryption itself."
            )


def gcs_cors_rule() -> dict[str, Any]:
    """The CORS rule, as Cloud Storage's JSON API writes it."""
    return {
        "origin": origins(),
        "method": ["GET", "PUT"],
        "responseHeader": ["content-type"],
        "maxAgeSeconds": 3600,
    }


def gcs_lifecycle_rules() -> list[dict[str, Any]]:
    """Abandoned multipart uploads aborted, and staging objects deleted, every version of
    them, after 2 days."""
    prefix = storage.staging_key("").rstrip("/") + "/"
    when = {"age": STALE_DAYS, "matchesPrefix": [prefix]}
    return [
        {"action": {"type": "AbortIncompleteMultipartUpload"}, "condition": when},
        {"action": {"type": "Delete"}, "condition": when},
    ]


def _get(read: Any) -> list[Any]:  # noqa: ANN401 - a call that may find no configuration
    try:
        return list(read())
    except storage.client().exceptions.ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in (
            "NoSuchCORSConfiguration",
            "NoSuchLifecycleConfiguration",
            "NoSuchBucketPolicy",
        ):
            return []
        raise


def _same(found: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Whether the store's copy says what Iki asked for (stores may add defaults)."""
    for key, value in wanted.items():
        have = found.get(key)
        if isinstance(value, list) and isinstance(have, list):
            if sorted(map(str, have)) != sorted(map(str, value)):
                return False
        elif isinstance(value, dict) and isinstance(have, dict):
            if not _same(have, value):
                return False
        elif have != value:
            return False
    return True
