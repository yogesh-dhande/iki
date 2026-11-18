"""Electronic signatures (ADR 0126) and the re-authentications that make them (ADR 0124).

A :class:`Signature` is one person's signature on one revision of a record, for one of the
signatures setting it to a state needs. What it means, who gave it and when are in its ``document``:
canonical JSON that this deployment's :class:`SigningKey` signed, which names the SHA-256 of
the record's content at that revision (``content``). Its columns repeat the document so it
can be queried; a verification checks they still agree.

Signatures, their withdrawals, people's acknowledgments and the organization's
certifications are only ever added to. A :class:`Signing` is the short-lived attempt that
makes one: started in the web app, finished when the identity provider says the person
signed in again.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7

#: The meanings a signature can have (21 CFR 11.50(a)(3)), as ``iki_spec`` names them.
MEANINGS = ("authorship", "review", "approval", "verification", "responsibility", "witness")


class SigningKey(models.Model):
    """One of this deployment's Ed25519 keys for signatures. The private key is encrypted
    with ``IKI_ENCRYPTION_KEYS``, so the database alone can't sign, and a key whose
    private half doesn't decrypt to its public one is trusted by nothing."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: ``ed25519:`` and its 32 bytes in unpadded base64url, as documents name it.
    public = models.CharField(max_length=64, unique=True)
    #: Its 32-byte seed, base64url, encrypted with Fernet (``core.crypto``).
    private_encrypted = models.TextField()
    created_at = models.DateTimeField(db_default=Now())
    #: Once retired, it signs nothing more, and still verifies what it signed.
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_signing_key"
        db_table_comment = "This deployment's keys for electronic signatures (ADR 0126)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["retired_at"],
                condition=Q(retired_at__isnull=True),
                name="iki_signing_key_one_current",
                nulls_distinct=False,
            ),
        ]

    def __str__(self) -> str:
        return self.public


class ForeignSigningKey(models.Model):
    """Another deployment's signing key, trusted for the signatures a bundle from it
    brought (ADR 0096). ``sealed`` is the key and deployment encrypted with
    ``IKI_ENCRYPTION_KEYS``: a row written by anything but Iki doesn't unseal."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    public = models.CharField(max_length=64, unique=True)
    deployment = models.CharField(max_length=100)
    sealed = models.TextField(
        help_text="Encrypted with IKI_ENCRYPTION_KEYS: the key and its deployment."
    )
    #: The import it came with.
    bundle_import = models.ForeignKey(
        "iki_bundles.BundleImport",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_signing_key_foreign"
        db_table_comment = "Other deployments' signing keys, for signatures bundles brought."

    def __str__(self) -> str:
        return f"{self.public} ({self.deployment})"


class Signature(models.Model):
    """A person's electronic signature on a revision of a record (ADR 0126)."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(
        "iki_entities.Entity", on_delete=models.PROTECT, related_name="signatures"
    )
    #: The record's revision it signs: what the signer read.
    revision = models.PositiveIntegerField()
    #: For a record of a registry entry (ADR 0115): the entry's revision then.
    entry_revision = models.PositiveIntegerField(null=True, blank=True)
    #: What it signs for: setting state field ``field`` to ``state``.
    field = models.CharField(max_length=63)
    state = models.CharField(max_length=50)
    #: Which of the signatures that needs it is, from 0.
    index = models.PositiveSmallIntegerField()
    meaning = models.CharField(max_length=20)
    signer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    #: The signer as the document names them: their ID where it was signed, which an
    #: imported signature's signer here may not have.
    signer_ref = models.CharField(max_length=64)
    signed_at = models.DateTimeField()
    #: The canonical JSON that was signed (``signatures.proof``).
    document = models.TextField()
    #: Ed25519, over ``document``'s bytes.
    signature = models.BinaryField()
    #: The key that signed it, as the document names it.
    key = models.CharField(max_length=64)
    #: The record's content as signed, canonical JSON: its SHA-256 is in the document.
    content = models.TextField()
    #: Where it was signed: this deployment's name, or the one a bundle brought it from.
    deployment = models.CharField(max_length=100)
    #: The import that brought it from another deployment (ADR 0096), if one did.
    bundle_import = models.ForeignKey(
        "iki_bundles.BundleImport",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    #: For an imported one: the registry entries its content names by their IDs where it
    #: was signed, by their IDs here, where those differ.
    renamed = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "iki_signature"
        db_table_comment = "Electronic signatures on records' revisions (21 CFR Part 11)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(meaning__in=MEANINGS), name="iki_signature_meaning"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="iki_signature_revision"),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "revision", "field", "state"], name="iki_signature_on"),
            models.Index(fields=["signer", "signed_at"], name="iki_signature_by"),
        ]

    def __str__(self) -> str:
        return f"{self.meaning} by {self.signer_id} on {self.entity_id} r{self.revision}"


class Withdrawal(models.Model):
    """A signer took back a signature before the state it signed for was set."""

    APPEND_ONLY = True

    signature = models.OneToOneField(
        Signature, primary_key=True, on_delete=models.PROTECT, related_name="withdrawal"
    )
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(blank=True, default="")
    withdrawn_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_signature_withdrawal"
        db_table_comment = (
            "Signatures their signers took back before the state they signed for was set."
        )

    def __str__(self) -> str:
        return f"Withdrawal of {self.signature_id}"


class Certification(models.Model):
    """The organization's certification to the FDA that its electronic signatures are the
    legally binding equivalent of handwritten ones (21 CFR 11.100(c)), as an Org Admin
    recorded it. The latest is the one in force; with none, nobody signs."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: When the organization sent its letter.
    submitted_on = models.DateField()
    #: Where it's kept, or how it was sent: "Letter of 2026-11-02 to FDA ORA, QA-DOC-0042".
    reference = models.CharField(max_length=500)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_signature_certification"
        db_table_comment = "The organization's Part 11 certifications, as Org Admins recorded them."

    def __str__(self) -> str:
        return f"Certified {self.submitted_on}"


class Acknowledgment(models.Model):
    """A person's acknowledgment, at their first signature, that it binds them as their
    handwritten signature would. Once each."""

    APPEND_ONLY = True

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The words they agreed to.
    statement = models.TextField()
    acknowledged_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_signature_acknowledgment"
        db_table_comment = "Each signer's acknowledgment that their signature binds them."

    def __str__(self) -> str:
        return f"Acknowledged by {self.user_id}"


class Status(models.TextChoices):
    #: Waiting for the identity provider.
    STARTED = "started", "Waiting for the sign-in"
    SIGNED = "signed", "Signed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class Signing(models.Model):
    """An attempt to sign: what's to be signed, by whom, in which browser session, until
    the identity provider answers or it expires (ADR 0124). Not a record: the audit log
    keeps what happened."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    #: SHA-256 of the session key it was started in: it finishes only there.
    session = models.CharField(max_length=64)
    entity = models.ForeignKey("iki_entities.Entity", on_delete=models.CASCADE, related_name="+")
    revision = models.PositiveIntegerField()
    entry_revision = models.PositiveIntegerField(null=True, blank=True)
    field = models.CharField(max_length=63)
    state = models.CharField(max_length=50)
    index = models.PositiveSmallIntegerField()
    reason = models.TextField(blank=True, default="")
    #: Where the web app wants the person back: a path on this site.
    return_to = models.CharField(max_length=2000)
    #: The acknowledgment they agreed to before their first signature, if this is it.
    acknowledged = models.TextField(blank=True, default="")
    provider = models.ForeignKey(
        "iki_accounts.IdentityProvider", on_delete=models.CASCADE, related_name="+"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STARTED)
    #: Why it failed, for the person.
    message = models.TextField(blank=True, default="")
    signature = models.OneToOneField(
        Signature, null=True, blank=True, on_delete=models.PROTECT, related_name="signing"
    )
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_signing"
        db_table_comment = "Attempts to sign, each waiting on the identity provider (ADR 0124)."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["user", "created_at"], name="iki_signing_by"),
        ]

    def __str__(self) -> str:
        return f"Signing {self.pk} ({self.status})"
