import os
import subprocess
import sys

import pytest

PROD_ENV = {
    "IKI_SECRET_KEY": "k" * 64 + "-not-a-real-secret-but-long-and-varied-0123456789",
    "IKI_ALLOWED_HOSTS": "iki.example.com",
    "IKI_ENVIRONMENT": "prod",
    "IKI_DATABASE_URL": "postgres://iki:pw@db.example.com:5432/iki",
    "IKI_ENCRYPTION_KEYS": "ZXhhbXBsZS1rZXktZm9yLXNldHRpbmdzLXRlc3RzLTE=",
    "DJANGO_SETTINGS_MODULE": "iki_server.settings",
}


def run_manage(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("IKI_", "DJANGO_"))}
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "iki_server.manage", *args],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_production_settings_pass_djangos_deployment_checks() -> None:
    result = run_manage("check", "--deploy", "--fail-level", "WARNING", env=PROD_ENV)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    "missing", ["IKI_SECRET_KEY", "IKI_ALLOWED_HOSTS", "IKI_DATABASE_URL", "IKI_ENCRYPTION_KEYS"]
)
def test_required_settings_fail_fast(missing: str) -> None:
    env = {k: v for k, v in PROD_ENV.items() if k != missing}
    result = run_manage("check", env=env)
    assert result.returncode != 0
    assert missing in result.stderr


def test_invalid_encryption_keys_are_rejected() -> None:
    result = run_manage("check", env={**PROD_ENV, "IKI_ENCRYPTION_KEYS": "not-a-fernet-key"})
    assert result.returncode != 0
    assert "IKI_ENCRYPTION_KEYS: key 1 is not a Fernet key" in result.stderr


def test_cookies_use_host_prefixes_over_https() -> None:
    from django.conf import settings

    assert settings.SESSION_COOKIE_NAME == "__Host-iki_session"
    assert settings.CSRF_COOKIE_NAME == "__Host-iki_csrf"


def test_non_postgres_database_is_rejected() -> None:
    result = run_manage("check", env={**PROD_ENV, "IKI_DATABASE_URL": "sqlite:///db.sqlite3"})
    assert result.returncode != 0
    assert "must point at PostgreSQL" in result.stderr


def test_debug_is_off_and_https_on_by_default() -> None:
    from django.conf import settings

    assert settings.DEBUG is False
    assert settings.SECURE_SSL_REDIRECT is True
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.DATABASES["default"]["CONN_MAX_AGE"] == 0


def test_production_uses_the_connection_pool_and_verified_tls() -> None:
    code = (
        "from django.conf import settings as s; o = s.DATABASES['default']['OPTIONS'];"
        "assert o['pool'] and o['pool']['max_size'] == 10, o;"
        "assert o['sslmode'] == 'verify-full', o"
    )
    result = run_manage("shell", "-c", code, env=PROD_ENV)
    assert result.returncode == 0, result.stderr


def test_the_web_app_dev_server_is_refused_over_https() -> None:
    result = run_manage("check", env={**PROD_ENV, "IKI_WEB_DEV_SERVER": "true"})
    assert result.returncode != 0
    assert "IKI_WEB_DEV_SERVER is for local development" in result.stderr
    local = {**PROD_ENV, "IKI_WEB_DEV_SERVER": "true", "IKI_HTTPS": "false"}
    assert run_manage("check", env=local).returncode == 0
