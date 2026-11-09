"""Application-level encryption for secrets stored in the database.

Secrets such as an identity provider's client secret are encrypted with
Fernet (AES-128-CBC plus HMAC-SHA256, from ``cryptography``) before they are
written, so a database dump or backup alone doesn't reveal them. This is on
top of storage encryption (KMS) in production.

Keys come from ``IKI_ENCRYPTION_KEYS``, a comma-separated list. The first key
encrypts; every key decrypts. To rotate: put a new key first, deploy, run
``iki-server rotate_encryption``, then drop the old key.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class DecryptionError(Exception):
    """A value couldn't be decrypted with any configured key."""


def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in settings.IKI_ENCRYPTION_KEYS])


def validate_keys(keys: list[str]) -> None:
    """Raises ``ValueError`` unless ``keys`` is a non-empty list of valid Fernet keys."""
    if not keys:
        raise ValueError("at least one key is required")
    for index, key in enumerate(keys):
        try:
            Fernet(key)
        except ValueError:
            raise ValueError(
                f"key {index + 1} is not a Fernet key (32 bytes, URL-safe base64)"
            ) from None


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise DecryptionError("no configured encryption key decrypts this value") from None


def rotate(token: str) -> str:
    """Re-encrypts ``token`` with the primary (first) key."""
    try:
        return _fernet().rotate(token.encode()).decode()
    except InvalidToken:
        raise DecryptionError("no configured encryption key decrypts this value") from None
