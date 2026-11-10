"""Django settings for the Iki server.

All configuration comes from environment variables prefixed with ``IKI_``,
so the same image runs in every deployment. Defaults are the secure choice:
anything that weakens security (``IKI_DEBUG``, plain HTTP) must be turned on
explicitly. Required values have no default and fail fast at startup.
"""

from pathlib import Path

import environ
from django.utils.csp import CSP

from iki_server import __version__
from iki_server.core.crypto import validate_keys as validate_encryption_keys
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

#: Public base URL of this deployment, e.g. ``https://iki.example.com``. Used
#: in links Iki prints or sends (setup links now; emails later).
IKI_PUBLIC_URL: str = env.str("IKI_PUBLIC_URL", default="")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "iki_server.core",
    # Before allauth, so Iki's templates override allauth's.
    "iki_server.web",
    "iki_server.accounts",
    "iki_server.org",
    "iki_server.audit",
    "iki_server.setup",
    "iki_server.scim",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.openid_connect",
    "allauth.socialaccount.providers.saml",
]

MIDDLEWARE = [
    # Health checks answer before host validation and HTTPS redirects, so
    # kubelet probes (which use the pod IP and plain HTTP) always reach them.
    "iki_server.health.HealthCheckMiddleware",
    "iki_server.observability.middleware.RequestLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "iki_server.web.middleware.SecurityHeadersMiddleware",
    "iki_server.web.csp.ContentSecurityPolicyMiddleware",
    "iki_server.api.middleware.ApiResponseMiddleware",
    "servestatic.middleware.ServeStaticMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "iki_server.audit.log.AuditContextMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "iki_server.accounts.session_policy.SessionPolicyMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "iki_server.urls"
ASGI_APPLICATION = "iki_server.asgi.application"
WSGI_APPLICATION = None

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "iki_server.web.context_processors.installation",
                "iki_server.org.context_processors.navigation",
            ],
        },
    }
]

# Confirmation messages ("Added Ada to Structural Biology") live in the
# server-side session, not in a cookie.
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

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

#: Refuse to turn ready when connected as a role that can change the schema
#: or the audit log (ADR 0025). Only the test suite, which owns its throwaway
#: database, turns this off.
IKI_DB_REQUIRE_LEAST_PRIVILEGE: bool = env.bool("IKI_DB_REQUIRE_LEAST_PRIVILEGE", default=True)

HTTPS: bool = env.bool("IKI_HTTPS", default=True)
SECURE_SSL_REDIRECT = HTTPS
SECURE_HSTS_SECONDS = 63_072_000 if HTTPS else 0  # two years
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS
SECURE_HSTS_PRELOAD = HTTPS
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
CSRF_TRUSTED_ORIGINS: list[str] = env.list("IKI_CSRF_TRUSTED_ORIGINS", default=[])
CSRF_FAILURE_VIEW = "iki_server.web.errors.csrf_failure"

# Content Security Policy (Django's built-in middleware, extended in
# iki_server.web.csp for the identity provider's form-action origin).
# Nothing inline, nothing from other origins.
SECURE_CSP = {
    "default-src": [CSP.NONE],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "font-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "manifest-src": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "object-src": [CSP.NONE],
    "require-trusted-types-for": ["'script'"],
    "trusted-types": [CSP.NONE],
    "upgrade-insecure-requests": HTTPS,
    "report-uri": ["/csp-report/"],
    "report-to": ["csp"],
}

# --- Encryption of stored secrets ------------------------------------------
# Comma-separated Fernet keys; the first encrypts, all decrypt. Generate one
# with: openssl rand -base64 32 | tr '+/' '-_'

IKI_ENCRYPTION_KEYS: list[str] = env.list("IKI_ENCRYPTION_KEYS")
try:
    validate_encryption_keys(IKI_ENCRYPTION_KEYS)
except ValueError as error:
    raise environ.ImproperlyConfigured(f"IKI_ENCRYPTION_KEYS: {error}") from None

# --- Sign-in and sessions ---------------------------------------------------
# Single sign-on only: there are no local passwords (see docs/adr/0023).

AUTH_USER_MODEL = "iki_accounts.User"
AUTHENTICATION_BACKENDS = ["allauth.account.auth_backends.AuthenticationBackend"]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "signed_out"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.ScryptPasswordHasher"]

#: API requests one caller may make per minute (ADR 0027).
IKI_API_RATE_LIMIT_PER_MINUTE: int = env.int("IKI_API_RATE_LIMIT_PER_MINUTE", default=600)

SESSION_ENGINE = "iki_server.accounts.sessions"
#: Idle timeout. The absolute limit is IKI_SESSION_MAX_AGE_SECONDS.
SESSION_COOKIE_AGE = env.int("IKI_SESSION_IDLE_TIMEOUT_MINUTES", default=8 * 60) * 60
IKI_SESSION_MAX_AGE_SECONDS: int = env.int("IKI_SESSION_MAX_AGE_HOURS", default=24) * 3600
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"  # sent on the identity provider's redirect back to Iki
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = HTTPS
# The __Host- prefix makes browsers refuse these cookies unless they are
# Secure, host-only and path=/, so no subdomain can set or overwrite them.
SESSION_COOKIE_NAME = "__Host-iki_session" if HTTPS else "iki_session"
CSRF_COOKIE_NAME = "__Host-iki_csrf" if HTTPS else "iki_csrf"

ACCOUNT_ADAPTER = "iki_server.accounts.adapters.AccountAdapter"
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_SESSION_REMEMBER = False
SOCIALACCOUNT_ONLY = True
SOCIALACCOUNT_ADAPTER = "iki_server.accounts.adapters.SocialAccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = False  # starting a sign-in takes a POST with a CSRF token
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False  # never link accounts by email
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_REQUESTS_TIMEOUT = 10
SOCIALACCOUNT_OPENID_CONNECT_URL_PREFIX = "oidc"

# --- Static files -------------------------------------------------------------
# Collected into the image at build time and served by ServeStatic with
# content-hashed names and long-lived caching.

STATIC_URL = "/static/"
STATIC_ROOT = env.path("IKI_STATIC_ROOT", default=str(BASE_DIR.parent / ".static"))
STORAGES = {
    "staticfiles": {"BACKEND": "servestatic.storage.CompressedManifestStaticFilesStorage"},
}

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
