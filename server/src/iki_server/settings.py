"""Django settings for the Iki server.

All configuration comes from environment variables prefixed with ``IKI_``,
so the same image runs in every deployment. Defaults are the secure choice:
anything that weakens security (``IKI_DEBUG``, plain HTTP) must be turned on
explicitly. Required values have no default and fail fast at startup.
"""

from pathlib import Path

import environ

from iki_server import __version__
from iki_server.observability.logging import configure_logging

env = environ.Env()

BASE_DIR = Path(__file__).resolve().parent

# --- Core -------------------------------------------------------------------

SECRET_KEY: str = env.str("IKI_SECRET_KEY")
DEBUG: bool = env.bool("IKI_DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("IKI_ALLOWED_HOSTS")

#: Name of this deployment (for example ``prod`` or ``dev``). Attached to logs and traces.
IKI_ENVIRONMENT: str = env.str("IKI_ENVIRONMENT")
IKI_VERSION: str = __version__

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "iki_server.core",
]

MIDDLEWARE = [
    # Health checks answer before host validation and HTTPS redirects, so
    # kubelet probes (which use the pod IP and plain HTTP) always reach them.
    "iki_server.health.HealthCheckMiddleware",
    "iki_server.observability.middleware.RequestLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "iki_server.urls"
ASGI_APPLICATION = "iki_server.asgi.application"
WSGI_APPLICATION = None

TEMPLATES: list[dict[str, object]] = []

# --- Database ---------------------------------------------------------------
# Postgres is the only supported database. Connections come from psycopg's
# pool, which is what Django recommends under ASGI. CONN_MAX_AGE must stay 0
# when the pool is on.

DATABASES = {
    "default": {
        **env.db_url("IKI_DATABASE_URL"),
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            # The test suite turns the pool off: pooled connections held by
            # other threads would block dropping the test database.
            "pool": (
                {
                    "min_size": env.int("IKI_DB_POOL_MIN_SIZE", default=2),
                    "max_size": env.int("IKI_DB_POOL_MAX_SIZE", default=10),
                    "timeout": env.float("IKI_DB_POOL_TIMEOUT_SECONDS", default=10.0),
                }
                if env.bool("IKI_DB_POOL", default=True)
                else False
            ),
            "connect_timeout": env.int("IKI_DB_CONNECT_TIMEOUT_SECONDS", default=5),
            "application_name": "iki-server",
            # verify-full checks the server certificate and hostname.
            "sslmode": env.str("IKI_DB_SSLMODE", default="verify-full"),
            **(
                {"sslrootcert": env.str("IKI_DB_SSLROOTCERT")}
                if env.str("IKI_DB_SSLROOTCERT", default="")
                else {}
            ),
        },
    }
}
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise environ.ImproperlyConfigured("IKI_DATABASE_URL must point at PostgreSQL")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- HTTP security ----------------------------------------------------------
# IKI_HTTPS=true (the default) means clients reach Iki over HTTPS, usually
# through a TLS-terminating load balancer. Only local development turns it off.

HTTPS: bool = env.bool("IKI_HTTPS", default=True)
SECURE_SSL_REDIRECT = HTTPS
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
SECURE_HSTS_SECONDS = 63_072_000 if HTTPS else 0  # two years
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS
SECURE_HSTS_PRELOAD = HTTPS
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS: list[str] = env.list("IKI_CSRF_TRUSTED_ORIGINS", default=[])

# --- Internationalization ---------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Logging ----------------------------------------------------------------
# JSON lines on stdout in every deployment. IKI_LOG_FORMAT=console gives
# human-readable output for local development.

LOGGING_CONFIG = None  # configure_logging() owns the logging setup
configure_logging(
    level=env.str("IKI_LOG_LEVEL", default="INFO"),
    fmt=env.str("IKI_LOG_FORMAT", default="json"),
)
