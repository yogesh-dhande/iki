"""Settings for building plugins, with no database: `iki plugin makemigrations` uses them.

They are the server's settings with Django's dummy database backend, so
migrations are made from the migration files alone and never need, or
touch, a database. Nothing configured here can serve requests or reach
anything: the keys are random and the deployment's environment is ignored.
"""

import base64
import os
import secrets

# Overwrite, not default: a developer's real configuration must never leak in.
os.environ.update(
    {
        "IKI_SECRET_KEY": secrets.token_urlsafe(50),
        "IKI_ALLOWED_HOSTS": "build.invalid",
        "IKI_ENVIRONMENT": "build",
        "IKI_DATABASE_URL": "postgres://build@build.invalid/build",
        "IKI_DB_SSLMODE": "disable",
        "IKI_ENCRYPTION_KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "OTEL_SDK_DISABLED": "true",
    }
)

from iki_server.settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
ALLOWED_HOSTS = []
