"""Settings for the test suite: production settings plus test-only environment defaults.

The database URL is a placeholder. ``conftest.py`` points it at a throwaway
Postgres container (or at ``IKI_TEST_DATABASE_URL`` in CI) before any test
database is created.
"""

import os

_TEST_ENV = {
    "IKI_SECRET_KEY": "test-only-" + "x" * 50,
    "IKI_ALLOWED_HOSTS": "testserver,localhost",
    "IKI_ENVIRONMENT": "test",
    "IKI_DATABASE_URL": "postgres://iki:iki@localhost:5432/iki",
    "IKI_DB_SSLMODE": "disable",
    "IKI_DB_POOL": "false",
    "IKI_HTTPS": "true",
    "OTEL_SDK_DISABLED": "false",
    # Test-only Fernet key (URL-safe base64 of 32 bytes).
    # Tests own their throwaway database and connect as its superuser.
    "IKI_DB_REQUIRE_LEAST_PRIVILEGE": "false",
    "IKI_ENCRYPTION_KEYS": "dGVzdC1vbmx5LWtleS1kby1ub3QtdXNlLWFueXdoZXI=",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

from iki_server.settings import *  # noqa: E402, F403

# Tests render templates without running collectstatic first.
STORAGES = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
# Serve static files straight from the apps' static/ directories.
SERVESTATIC_USE_FINDERS = True
SERVESTATIC_AUTOREFRESH = True
