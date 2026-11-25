"""This deployment's signing keys, and the keys it trusts for signatures (ADR 0126).

The first signature makes the deployment's key. Its private half is kept encrypted with
``IKI_ENCRYPTION_KEYS`` (Fernet), as identity providers' secrets are, so someone who can
write the database but doesn't hold those keys can neither sign with it nor plant a key
of their own: a key is trusted only once its private half decrypts and gives its public
one. Other deployments' keys, which bundles bring with their signatures, are sealed the
same way. ``iki-server signing_key`` shows the public key, and retires it for a new one.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from iki_server.bundles import keys
from iki_server.core import crypto
from iki_server.signatures.models import ForeignSigningKey, SigningKey

if TYPE_CHECKING:
    from iki_server.bundles.models import BundleImport


def current() -> tuple[SigningKey, Ed25519PrivateKey]:
    """The key that signs now, made the first time it's needed."""
    for _ in range(2):
        found = SigningKey.objects.filter(retired_at__isnull=True).first()
        if found is not None:
            private = _private(found.pk, found.private_encrypted)
            if private is None or keys.public_text(private) != found.public:
                raise crypto.DecryptionError(
                    "This deployment's signing key doesn't decrypt with IKI_ENCRYPTION_KEYS."
                )
            return found, private
        text, public = keys.generate()
        try:
            with transaction.atomic():
                SigningKey.objects.create(public=public, private_encrypted=crypto.encrypt(text))
        except IntegrityError:
            continue  # another signature made it first
    raise RuntimeError("Couldn't make this deployment's signing key.")  # pragma: no cover


def rotate() -> SigningKey:
    """Retires the current key; the next signature makes a new one."""
    with transaction.atomic():
        SigningKey.objects.filter(retired_at__isnull=True).update(retired_at=timezone.now())
        key, _ = current()
    return key


@cache
def _private(key_id: object, encrypted: str) -> Ed25519PrivateKey | None:
    try:
        return keys.private_key(crypto.decrypt(encrypted))
    except (crypto.DecryptionError, ValueError):
        return None


def seal(public: str, deployment: str) -> str:
    return crypto.encrypt(f"{public}\n{deployment}")


@cache
def _unseal(sealed: str) -> tuple[str, str] | None:
    try:
        public, _, deployment = crypto.decrypt(sealed).partition("\n")
    except crypto.DecryptionError:
        return None
    return public, deployment


def mine() -> set[str]:
    """This deployment's own keys, current and retired, whose private halves decrypt to
    them: what signs what only this deployment does, such as its migrations' changes to
    kept history (ADR 0148)."""
    return {
        key.public
        for key in SigningKey.objects.all()
        if (private := _private(key.pk, key.private_encrypted)) is not None
        and keys.public_text(private) == key.public
    }


def trusted() -> dict[str, str]:
    """Every key this deployment trusts for signatures, with the deployment it signs for:
    its own whose private halves decrypt to them, and others' that unseal."""
    out: dict[str, str] = {}
    for key in SigningKey.objects.all():
        private = _private(key.pk, key.private_encrypted)
        if private is not None and keys.public_text(private) == key.public:
            out[key.public] = settings.IKI_ENVIRONMENT
    for foreign in ForeignSigningKey.objects.all():
        unsealed = _unseal(foreign.sealed)
        if unsealed is not None and unsealed[0] == foreign.public:
            out.setdefault(foreign.public, unsealed[1])
    return out


def own() -> list[dict[str, str | None]]:
    """This deployment's keys, as a bundle lists them: each with when it was made and
    retired."""
    return [
        {
            "key": k.public,
            "created_at": k.created_at.isoformat(),
            "retired_at": k.retired_at.isoformat() if k.retired_at else None,
        }
        for k in SigningKey.objects.order_by("created_at")
    ]


def trust(public: str, deployment: str, bundle_import: BundleImport | None = None) -> bool:
    """Bind a new key, or check the binding another import already established."""
    keys.public_key(public)  # raises ValueError unless it's a key
    if SigningKey.objects.filter(public=public).exists():
        return trusted().get(public) == deployment
    foreign, _ = ForeignSigningKey.objects.get_or_create(
        public=public,
        defaults={
            "deployment": deployment,
            "sealed": seal(public, deployment),
            "bundle_import": bundle_import,
        },
    )
    return _unseal(foreign.sealed) == (public, deployment)
