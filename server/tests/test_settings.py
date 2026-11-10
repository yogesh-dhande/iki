import os
import subprocess
import sys
from pathlib import Path

import pytest

from iki_server import __version__

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


def _email_settings(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_manage(
        "shell",
        "-c",
        "from django.conf import settings as s; "
        "o = s.MAILERS.get('default', {}).get('OPTIONS', {}); "
        "print(s.IKI_EMAIL_ENABLED, *(o.get(k) for k in ('host', 'port', 'use_tls', 'use_ssl', "
        "'username', 'password')), s.DEFAULT_FROM_EMAIL)",
        env={**PROD_ENV, **env},
    )


def test_email_is_off_until_configured() -> None:
    result = _email_settings({})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1].split()[0] == "False"
    assert "MAILERS" not in result.stderr


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "smtp+tls://AKIA%2Fx:p%40ss@email-smtp.eu-west-1.amazonaws.com",
            "True email-smtp.eu-west-1.amazonaws.com 587 True False AKIA/x p@ss "
            "Iki <iki@example.com>",
        ),
        (
            "smtp+ssl://u:p@smtp.example.com",
            "True smtp.example.com 465 False True u p Iki <iki@example.com>",
        ),
    ],
)
def test_email_needs_encryption_and_a_sender(url: str, expected: str) -> None:
    result = _email_settings({"IKI_EMAIL_URL": url, "IKI_EMAIL_FROM": "Iki <iki@example.com>"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == expected

    no_sender = _email_settings({"IKI_EMAIL_URL": url})
    assert no_sender.returncode != 0
    assert "IKI_EMAIL_FROM" in no_sender.stderr


def test_unencrypted_email_needs_an_explicit_choice() -> None:
    plain = {"IKI_EMAIL_URL": "smtp://relay.corp.example:25", "IKI_EMAIL_FROM": "iki@corp.example"}
    refused = _email_settings(plain)
    assert refused.returncode != 0
    assert "sends mail without encryption" in refused.stderr
    assert _email_settings({**plain, "IKI_EMAIL_ALLOW_PLAINTEXT": "true"}).returncode == 0
    assert _email_settings({**plain, "IKI_HTTPS": "false"}).returncode == 0
    # smtps:// means different things to different tools, so it's refused.
    ambiguous = _email_settings({**plain, "IKI_EMAIL_URL": "smtps://u:p@smtp.example.com"})
    assert ambiguous.returncode != 0
    assert "smtp+tls://" in ambiguous.stderr


def _plugins(tmp_path: Path, body: str) -> dict[str, str]:
    package = tmp_path / "iki_installed_plugins"
    package.mkdir()
    (package / "__init__.py").write_text(body + "APPS = []\n")
    return {**PROD_ENV, "PYTHONPATH": str(package.parent)}


def test_plugins_built_by_another_iki_are_refused(tmp_path: Path) -> None:
    env = _plugins(tmp_path, "IKI_VERSION = '0.0.9'\nLOCAL = False\n")
    result = run_manage("check", env=env)
    assert result.returncode != 0
    assert f"The plugins were built by Iki 0.0.9, but this is Iki {__version__}." in result.stderr


def test_plugins_built_by_this_iki_are_installed(tmp_path: Path) -> None:
    env = _plugins(tmp_path, f"IKI_VERSION = {__version__!r}\nLOCAL = False\n")
    result = run_manage("check", env=env)
    assert result.returncode == 0, result.stderr


def test_local_plugins_need_a_development_server(tmp_path: Path) -> None:
    env = _plugins(tmp_path, f"IKI_VERSION = {__version__!r}\nLOCAL = True\n")
    result = run_manage("check", env=env)
    assert result.returncode != 0
    assert "only for development" in result.stderr
    result = run_manage("check", env={**env, "IKI_ALLOW_LOCAL_PLUGINS": "true"})
    assert result.returncode == 0, result.stderr
