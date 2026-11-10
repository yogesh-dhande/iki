"""Signing messages as the Standard Webhooks spec says (standardwebhooks.com).

Each message carries ``webhook-id``, ``webhook-timestamp`` and
``webhook-signature`` headers. The signature is ``v1,`` and the base64
HMAC-SHA256 of ``{id}.{timestamp}.{body}``, keyed with the secret's bytes.
During a secret rotation the header holds two signatures, space-separated,
so a receiver with either secret accepts it. Receivers should refuse
timestamps more than five minutes old, which stops replays.
"""

import base64
import hashlib
import hmac
import secrets

PREFIX = "whsec_"
#: 32 random bytes: 256 bits, the HMAC-SHA256 key size.
SECRET_BYTES = 32


def new_secret() -> str:
    return PREFIX + base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode()


def _key(secret: str) -> bytes:
    if not secret.startswith(PREFIX):
        raise ValueError("Webhook secrets start with whsec_.")
    return base64.b64decode(secret.removeprefix(PREFIX), validate=True)


def signature(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def headers(secrets_: list[str], message_id: str, timestamp: int, body: bytes) -> dict[str, str]:
    """The headers for one attempt, signed with each secret given (newest first)."""
    return {
        "webhook-id": message_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": " ".join(
            signature(secret, message_id, timestamp, body) for secret in secrets_
        ),
    }


def verify(secret: str, headers_: dict[str, str], body: bytes) -> bool:
    """What a receiver does (used by tests and the docs' example)."""
    expected = signature(secret, headers_["webhook-id"], int(headers_["webhook-timestamp"]), body)
    return any(
        hmac.compare_digest(expected, given) for given in headers_["webhook-signature"].split()
    )
