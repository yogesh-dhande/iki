"""Django settings for the Iki server.

All configuration comes from environment variables prefixed with ``IKI_``,
so the same image runs in every deployment. Defaults are the secure choice:
anything that weakens security (``IKI_DEBUG``, plain HTTP) must be turned on
explicitly. Required values have no default and fail fast at startup.
"""

import ipaddress
import os
from importlib.util import find_spec
from pathlib import Path
from typing import Any

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
#: in links Iki prints or sends (setup links, emails).
IKI_PUBLIC_URL: str = env.str("IKI_PUBLIC_URL", default="")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "iki_server.core",
    # Before allauth, so Iki's templates override allauth's.
    "iki_server.web",
    "iki_server.accounts",
    "iki_server.org",
    "iki_server.audit",
    "iki_server.setup",
    "iki_server.scim",
    "iki_server.oauth",
    "iki_server.tasks",
    "iki_server.realtime",
    "iki_server.notifications",
    "iki_server.webhooks",
    "iki_server.files",
    "iki_server.entities",
    "iki_server.plugins",
    "oauth2_provider",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.openid_connect",
    "allauth.socialaccount.providers.saml",
]

# Plugins (ADR 0006): `iki plugin build` generates a Django app for each, and
# the `iki_installed_plugins` package that lists them in dependency order.
# Without that package (development, tests), no plugins are installed.
#
# The plugins must have been built by this version of Iki, since the models
# and migrations it generates change between versions. Plugins from a local
# directory rather than a pinned commit (`iki deploy build --allow-local`) are
# for development, so the server only accepts them when told to.
IKI_ALLOW_LOCAL_PLUGINS: bool = env.bool("IKI_ALLOW_LOCAL_PLUGINS", default=False)
if find_spec("iki_installed_plugins") is not None:
    import iki_installed_plugins as _plugins  # type: ignore[import-not-found]

    _built_by = getattr(_plugins, "IKI_VERSION", "an older version")
    if _built_by != __version__:
        raise environ.ImproperlyConfigured(
            f"The plugins were built by Iki {_built_by}, but this is Iki {__version__}. "
            f"Build them with `iki deploy build` from Iki {__version__}."
        )
    if getattr(_plugins, "LOCAL", False) and not IKI_ALLOW_LOCAL_PLUGINS:
        raise environ.ImproperlyConfigured(
            "The plugins include one from a local directory, which is only for development. "
            "Pin it to a git commit, or set IKI_ALLOW_LOCAL_PLUGINS=true on a development server."
        )
    INSTALLED_APPS += _plugins.APPS

MIDDLEWARE = [
    # Health checks answer before host validation and HTTPS redirects, so
    # kubelet probes (which use the pod IP and plain HTTP) always reach them.
    "iki_server.health.HealthCheckMiddleware",
    "iki_server.observability.middleware.RequestLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "iki_server.web.middleware.SecurityHeadersMiddleware",
    "iki_server.web.csp.ContentSecurityPolicyMiddleware",
    "iki_server.api.middleware.ApiResponseMiddleware",
    # servestatic, also caching the web app's Vite-hashed files for ever.
    "iki_server.web.frontend.ServeStaticMiddleware",
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
                "iki_server.notifications.context_processors.unread",
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
if not HTTPS:
    # oauthlib refuses plain http unless told otherwise; only local
    # development runs without HTTPS, and there everything is plain http.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
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

# --- OAuth 2.1 server (ADR 0028) ------------------------------------------------

OAUTH2_PROVIDER_APPLICATION_MODEL = "iki_oauth.Client"
#: Fetch Client ID Metadata Documents from clients' https URLs. Needs egress
#: to the internet, so the Helm chart turns it on only when asked.
IKI_OAUTH_CLIENT_METADATA_DOCUMENTS: bool = env.bool(
    "IKI_OAUTH_CLIENT_METADATA_DOCUMENTS", default=False
)
#: DOT's deploy check refuses any "http" in ALLOWED_REDIRECT_URI_SCHEMES, which
#: would also refuse native apps' loopback callbacks (RFC 8252 §7.3).
#: iki_server.oauth.redirects allows http only to loopback hosts, at
#: registration and at every authorization request, which is what §2.1 asks.
SILENCED_SYSTEM_CHECKS = ["oauth2_provider.E003"]
OAUTH2_PROVIDER: dict[str, object] = {
    "SCOPES_BACKEND_CLASS": "iki_server.oauth.scopes.IkiScopes",
    "OAUTH2_VALIDATOR_CLASS": "iki_server.oauth.validators.IkiValidator",
    "SCOPES": {},  # IkiScopes supplies them
    "DEFAULT_SCOPES": [],
    # Short-lived access tokens; refresh tokens rotate, and reusing an old one
    # revokes the whole family (RFC 9700 §4.14).
    "ACCESS_TOKEN_EXPIRE_SECONDS": env.int("IKI_OAUTH_ACCESS_TOKEN_MINUTES", default=60) * 60,
    "REFRESH_TOKEN_EXPIRE_SECONDS": env.int("IKI_OAUTH_REFRESH_TOKEN_DAYS", default=30) * 86400,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,
    "PKCE_REQUIRED": True,
    "REQUEST_APPROVAL_PROMPT": "force",
    # Native apps redirect to http://127.0.0.1:<any port> (RFC 8252 §7.3).
    "ALLOW_LOCALHOST_LOOPBACK": True,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http"],
    "ALLOW_URI_WILDCARDS": False,
    "CLIENT_SECRET_GENERATOR_LENGTH": 64,
    "DEVICE_FLOW_INTERVAL": 5,
    # IkiValidator.validate_device_code sets the user and scopes, after its checks.
    "OAUTH_PRE_TOKEN_VALIDATION": [],
    # RFC 8628 wants an absolute URL; IKI_PUBLIC_URL is required in production.
    "OAUTH_DEVICE_VERIFICATION_URI": f"{IKI_PUBLIC_URL.rstrip('/')}/oauth/device/",
    # Every RFC 9700 recommendation, enforced.
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
    "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
    "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
    "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
    "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
    # Self-registration: anyone may register; an Org Admin approves (iki_server.oauth).
    "DCR_ENABLED": True,
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.dcr.AllowAllDCRPermission",),
    "CIMD_ENABLED": IKI_OAUTH_CLIENT_METADATA_DOCUMENTS,
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    "OAUTH2_GRANT_TYPES_SUPPORTED": [
        "authorization_code",
        "refresh_token",
        "client_credentials",
        "urn:ietf:params:oauth:grant-type:device_code",
    ],
    "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": ["none", "client_secret_basic"],
    "OAUTH2_PROTECTED_RESOURCE_BEARER_METHODS_SUPPORTED": ["header"],
    "OAUTH2_PROTECTED_RESOURCE_NAME": "Iki",
}

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

# --- Email ------------------------------------------------------------------
# IKI_EMAIL_URL turns email notifications on:
#   smtp+tls://user:password@email-smtp.eu-west-1.amazonaws.com:587  (STARTTLS)
#   smtp+ssl://user:password@smtp.example.com:465                    (TLS from the start)
# Plain smtp:// sends credentials and mail unencrypted, so it's refused
# unless IKI_HTTPS is off (local development) or IKI_EMAIL_ALLOW_PLAINTEXT
# says the relay is inside your own network. Percent-encode special
# characters in the user and password.

_EMAIL_URL = env.str("IKI_EMAIL_URL", default="")
IKI_EMAIL_ENABLED: bool = bool(_EMAIL_URL)
if IKI_EMAIL_ENABLED:
    _scheme = _EMAIL_URL.split("://", 1)[0]
    if _scheme not in ("smtp", "smtp+tls", "smtp+ssl"):
        raise environ.ImproperlyConfigured(
            "IKI_EMAIL_URL must start with smtp+tls:// (STARTTLS), smtp+ssl:// (TLS from the "
            "start) or smtp:// (no encryption)."
        )
    if _scheme == "smtp" and HTTPS and not env.bool("IKI_EMAIL_ALLOW_PLAINTEXT", default=False):
        raise environ.ImproperlyConfigured(
            "IKI_EMAIL_URL sends mail without encryption. Use smtp+tls:// or smtp+ssl://, or "
            "set IKI_EMAIL_ALLOW_PLAINTEXT=true for a relay inside your own network."
        )
    _email = env.email_url_config(_EMAIL_URL)
    MAILERS: dict[str, dict[str, Any]] = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": _email["EMAIL_HOST"] or "",
                "port": _email["EMAIL_PORT"] or {"smtp+ssl": 465, "smtp+tls": 587}.get(_scheme, 25),
                "username": _email["EMAIL_HOST_USER"] or "",
                "password": _email["EMAIL_HOST_PASSWORD"] or "",
                "use_tls": _scheme == "smtp+tls",
                "use_ssl": _scheme == "smtp+ssl",
                "timeout": 20,
            },
        }
    }
    #: The sender, such as ``Iki <iki@example.com>``. Use an address your mail
    #: system lets Iki send as (SPF, DKIM), or mail lands in spam.
    DEFAULT_FROM_EMAIL: str = env.str("IKI_EMAIL_FROM")
else:
    # Iki sends nothing: notifications stay in Iki. Django's deployment check
    # flags the dummy backend (mail.E001); here it's the operator's choice.
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.dummy.EmailBackend"}}
    SILENCED_SYSTEM_CHECKS.append("mail.E001")

# --- Webhooks -------------------------------------------------------------------
# Webhooks go to public HTTPS addresses only (see webhooks/sender.py). For
# receivers inside your network, list their networks (CIDRs, comma-separated)
# and, if their certificates come from an internal CA, give its bundle.

try:
    IKI_WEBHOOK_ALLOWED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        ipaddress.ip_network(network.strip())
        for network in env.list("IKI_WEBHOOK_ALLOWED_NETWORKS", default=[])
        if network.strip()
    ]
except ValueError as _error:
    raise environ.ImproperlyConfigured(
        f"IKI_WEBHOOK_ALLOWED_NETWORKS must be CIDRs such as 10.20.0.0/16: {_error}"
    ) from None
#: Plain http:// receivers: for development, never production.
IKI_WEBHOOK_ALLOW_HTTP: bool = env.bool("IKI_WEBHOOK_ALLOW_HTTP", default=False)
if IKI_WEBHOOK_ALLOW_HTTP and HTTPS:
    raise environ.ImproperlyConfigured(
        "IKI_WEBHOOK_ALLOW_HTTP sends webhooks unencrypted; it's only for development "
        "(IKI_HTTPS=false). Give internal receivers HTTPS, with IKI_WEBHOOK_CA_FILE for "
        "an internal CA."
    )
IKI_WEBHOOK_CA_FILE: str = env.str("IKI_WEBHOOK_CA_FILE", default="")
if IKI_WEBHOOK_CA_FILE and not Path(IKI_WEBHOOK_CA_FILE).is_file():
    raise environ.ImproperlyConfigured(f"IKI_WEBHOOK_CA_FILE: no file at {IKI_WEBHOOK_CA_FILE}")

# --- Files ------------------------------------------------------------------
# Object storage for entities' files (ADR 0036): S3, or an S3-compatible store
# such as SeaweedFS in development. Credentials come from the standard AWS
# chain (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or the pod's IAM role).
# Without a bucket, the files API answers 503.

IKI_FILES_BUCKET: str = env.str("IKI_FILES_BUCKET", default="")
#: Every key Iki writes starts with this, so one bucket can serve several deployments.
IKI_FILES_PREFIX: str = env.str("IKI_FILES_PREFIX", default="").strip("/")
IKI_FILES_REGION: str = env.str("IKI_FILES_REGION", default="us-east-1")
#: For S3-compatible stores; empty for AWS S3.
IKI_FILES_ENDPOINT_URL: str = env.str("IKI_FILES_ENDPOINT_URL", default="")
#: Where browsers and workers reach the store, if not the same address (signed URLs name it).
IKI_FILES_PUBLIC_ENDPOINT_URL: str = env.str(
    "IKI_FILES_PUBLIC_ENDPOINT_URL", default=IKI_FILES_ENDPOINT_URL
)
try:
    from iki_spec.model import parse_size as _parse_size

    IKI_FILES_MAX_UPLOAD_BYTES: int = _parse_size(
        env.str("IKI_FILES_MAX_UPLOAD_SIZE", default="100GiB")
    )
except ValueError as _error:
    raise environ.ImproperlyConfigured(
        f"IKI_FILES_MAX_UPLOAD_SIZE must be a size such as 100GiB: {_error}"
    ) from None
for _name, _url in (
    ("IKI_FILES_ENDPOINT_URL", IKI_FILES_ENDPOINT_URL),
    ("IKI_FILES_PUBLIC_ENDPOINT_URL", IKI_FILES_PUBLIC_ENDPOINT_URL),
):
    if _url.startswith("http://") and HTTPS:
        raise environ.ImproperlyConfigured(
            f"{_name} is plain HTTP, so files would travel unencrypted; it's only for "
            "development (IKI_HTTPS=false)."
        )
    if _url and not _url.startswith(("http://", "https://")):
        raise environ.ImproperlyConfigured(f"{_name} must be a URL such as https://s3.example.com")
if IKI_FILES_BUCKET:
    # Browsers send upload parts straight to the store, so pages may connect to it (only).
    from urllib.parse import urlsplit as _urlsplit

    _store = _urlsplit(IKI_FILES_PUBLIC_ENDPOINT_URL)
    SECURE_CSP["connect-src"] = [
        CSP.SELF,
        f"{_store.scheme}://{_store.netloc}"
        if IKI_FILES_PUBLIC_ENDPOINT_URL
        else f"https://{IKI_FILES_BUCKET}.s3.{IKI_FILES_REGION}.amazonaws.com",
    ]

# --- Background tasks -------------------------------------------------------
# Django's Tasks framework on Postgres; ``iki-server tasks`` runs them.

TASKS = {
    "default": {
        "BACKEND": "iki_server.tasks.backend.PostgresBackend",
        "QUEUES": ["default"],
    }
}

# --- Static files -------------------------------------------------------------
# Collected into the image at build time and served by ServeStatic with
# content-hashed names and long-lived caching.

STATIC_URL = "/static/"
STATIC_ROOT = env.path("IKI_STATIC_ROOT", default=str(BASE_DIR.parent / ".static"))
STORAGES = {
    "staticfiles": {"BACKEND": "iki_server.web.frontend.StaticFilesStorage"},
}

#: Development only: pages requested through the web app's Vite dev server
#: (``make web-dev``) load the app's modules from it. Refused with HTTPS on.
IKI_WEB_DEV_SERVER: bool = env.bool("IKI_WEB_DEV_SERVER", default=False)
if IKI_WEB_DEV_SERVER and HTTPS:
    raise environ.ImproperlyConfigured(
        "IKI_WEB_DEV_SERVER is for local development, which runs without HTTPS (IKI_HTTPS=false)."
    )

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
