"""Identifiers and encryption of stored secrets."""

import uuid
from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command

from iki_server.core import crypto, ids


def test_uuid7_is_time_ordered_and_well_formed() -> None:
    values = [ids.uuid7() for _ in range(1000)]
    assert all(v.version == 7 and v.variant == uuid.RFC_4122 for v in values)
    # Strictly increasing, even within a millisecond: lists page by them.
    assert values == sorted(set(values))


def test_uuid7_fallback_for_python_before_3_14(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(uuid, "uuid7", raising=False)
    values = [ids.uuid7() for _ in range(1000)]
    assert all(v.version == 7 and v.variant == uuid.RFC_4122 for v in values)
    assert values == sorted(set(values))


def test_uuid7_fallback_keeps_order_when_the_clock_goes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.delattr(uuid, "uuid7", raising=False)
    now = time.time_ns()
    monkeypatch.setattr(time, "time_ns", lambda: now)
    first = ids.uuid7()
    monkeypatch.setattr(time, "time_ns", lambda: now - 5_000_000_000)
    second = ids.uuid7()
    assert first < second and second.version == 7


def test_uuid7_fallback_moves_on_a_millisecond_when_its_counter_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.delattr(uuid, "uuid7", raising=False)
    now = time.time_ns() + 10_000_000_000
    monkeypatch.setattr(time, "time_ns", lambda: now)
    first = ids.uuid7()
    monkeypatch.setattr(ids, "_counter", ids._COUNTER_MAX)
    second = ids.uuid7()
    assert first < second and second.int >> 80 == (first.int >> 80) + 1


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
def test_rotate_encryption_re_encrypts_every_stored_secret(settings: object) -> None:
    from iki_server.accounts.models import IdentityProvider
    from iki_server.audit.models import AuditEvent
    from iki_server.webhooks.models import Webhook

    provider = IdentityProvider(
        display_name="SSO",
        issuer="https://login.example.com",
        client_id="iki",
        authorization_endpoint="https://login.example.com/authorize",
    )
    provider.client_secret = "s3cret"
    provider.save()
    # A webhook mid-rotation holds two secrets; one that isn't, only its own.
    rotating = Webhook.objects.create(
        url="https://lims.example.com/hook",
        events=["run.finished"],
        secret=crypto.encrypt("whsec_new"),
        previous_secret=crypto.encrypt("whsec_old"),
    )
    plain = Webhook.objects.create(
        url="https://eln.example.com/hook",
        events=["run.finished"],
        secret=crypto.encrypt("whsec_only"),
    )
    new_key = Fernet.generate_key().decode()
    settings.IKI_ENCRYPTION_KEYS = [new_key, *settings.IKI_ENCRYPTION_KEYS]  # type: ignore[attr-defined]
    out = StringIO()
    call_command("rotate_encryption", stdout=out)
    assert "Re-encrypted 4 secret(s)" in out.getvalue()
    # With the old key retired, every one still reads.
    settings.IKI_ENCRYPTION_KEYS = [new_key]  # type: ignore[attr-defined]
    provider.refresh_from_db()
    assert provider.client_secret == "s3cret"
    rotating.refresh_from_db()
    plain.refresh_from_db()
    assert crypto.decrypt(rotating.secret) == "whsec_new"
    assert crypto.decrypt(rotating.previous_secret) == "whsec_old"
    assert crypto.decrypt(plain.secret) == "whsec_only"
    assert plain.previous_secret == ""
    assert AuditEvent.objects.get(action="encryption.rotated").data == {"secrets": 4}


def test_every_encrypted_field_is_one_rotation_re_encrypts() -> None:
    """A field named or described as encrypted that ENCRYPTED leaves out couldn't be read
    once its key is retired."""
    from django.apps import apps

    found = {
        (model._meta.label, field.name)
        for model in apps.get_models()
        if model._meta.app_label.startswith("iki_")
        for field in model._meta.get_fields()
        if field.name.endswith("_encrypted")
        or str(getattr(field, "help_text", "")).startswith("Encrypted")
    }
    assert found == set(crypto.ENCRYPTED)


@pytest.mark.django_db
def test_models_and_migrations_are_in_sync() -> None:
    call_command("makemigrations", "--check", "--dry-run", stdout=StringIO())
