"""A deployment's bundle signing key, and the keys of the deployments it takes bundles from.

Ed25519 (ADR 0096). A private key is written as its 32-byte seed in base64url, as
``IKI_BUNDLE_SIGNING_KEY`` holds it. A public key is ``ed25519:`` and its 32 bytes in
unpadded base64url, as a bundle and ``IKI_BUNDLE_TRUSTED_KEYS`` give it.

This module reads no settings of its own, so the settings module can check keys with it.
"""

from __future__ import annotations

import base64
import binascii
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "ed25519:"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _bytes(text: str, what: str) -> bytes:
    text = text.strip()
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{what} isn't base64url") from None
    if len(raw) != 32:
        raise ValueError(f"{what} is {len(raw)} bytes, not Ed25519's 32")
    return raw


def _text(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def private_key(text: str) -> Ed25519PrivateKey:
    """The key ``IKI_BUNDLE_SIGNING_KEY`` holds, or ValueError saying what's wrong."""
    return Ed25519PrivateKey.from_private_bytes(_bytes(text, "The signing key"))


def public_text(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return PREFIX + _text(raw)


def public_key(text: str) -> Ed25519PublicKey:
    if not text.startswith(PREFIX):
        raise ValueError(f"A public key starts with {PREFIX}")
    return Ed25519PublicKey.from_public_bytes(_bytes(text[len(PREFIX) :], "The public key"))


def generate() -> tuple[str, str]:
    """A new key: its private text, for the deployment's secret, and its public text."""
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return _text(raw), public_text(key)


def trusted(text: str) -> dict[str, tuple[str, ...]]:
    """The deployments ``IKI_BUNDLE_TRUSTED_KEYS`` names, each with its keys.

    It's comma-separated ``name=ed25519:...`` pairs. A name can be given more than once,
    for a key being rotated.
    """
    out: dict[str, list[str]] = {}
    for item in filter(None, (i.strip() for i in text.split(","))):
        name, equals, key = (part.strip() for part in item.partition("="))
        if not equals or not _NAME.fullmatch(name):
            raise ValueError(
                f"{item!r} isn't name=ed25519:key, with a name such as staging (letters, digits, "
                "dots, dashes and underscores)"
            )
        public_key(key)
        out.setdefault(name, []).append(key)
    return {name: tuple(keys) for name, keys in out.items()}


def sign(key: Ed25519PrivateKey, data: bytes) -> bytes:
    return key.sign(data)


def verify(public: str, signature: bytes, data: bytes) -> bool:
    try:
        public_key(public).verify(signature, data)
    except (InvalidSignature, ValueError):
        return False
    return True
