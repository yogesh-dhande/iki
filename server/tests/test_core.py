"""Identifiers and encryption of stored secrets."""

import uuid
from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command

from iki_server.core import crypto, ids


def test_uuid7_is_time_ordered_and_well_formed() -> None:
    values = [ids.uuid7() for _ in range(50)]
    assert all(v.version == 7 and v.variant == uuid.RFC_4122 for v in values)
    assert len(set(values)) == 50


def test_uuid7_fallback_for_python_before_3_14(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(uuid, "uuid7", raising=False)
    first, second = ids.uuid7(), ids.uuid7()
    assert first.version == second.version == 7
    assert first.variant == uuid.RFC_4122
    assert first.int >> 80 <= second.int >> 80  # millisecond timestamp prefix


def test_encryption_round_trip_hides_the_plaintext() -> None:
    token = crypto.encrypt("client-secret")
    assert "client-secret" not in token
    assert crypto.decrypt(token) == "client-secret"


def test_key_rotation(settings: object) -> None:
    old_key = settings.IKI_ENCRYPTION_KEYS[0]  # type: ignore[attr-defined]
    token = crypto.encrypt("client-secret")
    new_key = Fernet.generate_key().decode()
    settings.IKI_ENCRYPTION_KEYS = [new_key, old_key]  # type: ignore[attr-defined]
    rotated = crypto.rotate(token)
    settings.IKI_ENCRYPTION_KEYS = [new_key]  # type: ignore[attr-defined]
    assert crypto.decrypt(rotated) == "client-secret"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token)


@pytest.mark.parametrize("keys", [[], ["not-a-key"], [Fernet.generate_key().decode(), "short"]])
def test_invalid_keys_are_rejected(keys: list[str]) -> None:
    with pytest.raises(ValueError, match="key"):
        crypto.validate_keys(keys)


@pytest.mark.django_db
def test_rotate_encryption_command_re_encrypts_stored_secrets(settings: object) -> None:
    from iki_server.accounts.models import IdentityProvider

    provider = IdentityProvider(
        display_name="SSO",
        issuer="https://login.example.com",
        client_id="iki",
        authorization_endpoint="https://login.example.com/authorize",
    )
    provider.client_secret = "s3cret"
    provider.save()
    new_key = Fernet.generate_key().decode()
    settings.IKI_ENCRYPTION_KEYS = [new_key, *settings.IKI_ENCRYPTION_KEYS]  # type: ignore[attr-defined]
    out = StringIO()
    call_command("rotate_encryption", stdout=out)
    assert "Re-encrypted 1 secret" in out.getvalue()
    settings.IKI_ENCRYPTION_KEYS = [new_key]  # type: ignore[attr-defined]
    provider.refresh_from_db()
    assert provider.client_secret == "s3cret"


@pytest.mark.django_db
def test_models_and_migrations_are_in_sync() -> None:
    call_command("makemigrations", "--check", "--dry-run", stdout=StringIO())
