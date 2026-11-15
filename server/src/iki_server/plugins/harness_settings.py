"""Settings for ``iki plugin test --upgrade-from``: the server's, on a scratch database.

The CLI makes the database, passes it as ``IKI_HARNESS_DATABASE_URL`` and drops it after.
As with :mod:`iki_server.plugins.build_settings`, nothing here can serve requests: the keys
are random, and a developer's own configuration is overwritten, never read.
"""

import base64
import os
import secrets

os.environ.update(
    {
        "IKI_SECRET_KEY": secrets.token_urlsafe(50),
        "IKI_ALLOWED_HOSTS": "harness.invalid",
        "IKI_ENVIRONMENT": "harness",
        "IKI_DATABASE_URL": os.environ["IKI_HARNESS_DATABASE_URL"],
        "IKI_DB_SSLMODE": os.environ.get("IKI_HARNESS_DB_SSLMODE", "prefer"),
        "IKI_DB_POOL": "false",
        # A scratch database, owned by whoever runs it.
        "IKI_DB_REQUIRE_LEAST_PRIVILEGE": "false",
        "IKI_ENCRYPTION_KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "OTEL_SDK_DISABLED": "true",
        "IKI_ALLOW_LOCAL_PLUGINS": "true",
    }
)

from iki_server.settings import *  # noqa: F403

ALLOWED_HOSTS = []
