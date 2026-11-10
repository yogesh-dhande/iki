"""People, their sessions, and the identity provider they sign in with.

Iki has no local passwords. Everyone signs in through the organization's
identity provider (OIDC now, SAML in a later release), and a user record is
created the first time someone signs in.
"""

from typing import Any, ClassVar
from urllib.parse import urlsplit

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.sessions.base_session import AbstractBaseSession
from django.db import models
from django.db.models import Q
from django.utils import timezone

from iki_server.core import crypto
from iki_server.core.ids import uuid7

#: Case-insensitive ICU collation, created by the first migration. Email
#: addresses compare equal regardless of case, while keeping the case the
#: identity provider sent for display.
CASE_INSENSITIVE = "iki_case_insensitive"


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, email: str, name: str = "", **extra: Any) -> "User":  # noqa: ANN401
        if not email:
            raise ValueError("users need an email address")
        user = self.model(email=self.normalize_email(email), name=name, **extra)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, *args: object, **kwargs: object) -> "User":
        raise NotImplementedError(
            "Iki has no local passwords. Run `iki-server setup_link` to create the "
            "first administrator through your identity provider."
        )


class User(AbstractBaseUser):
    """Someone who signs in through the identity provider.

    What they may do comes only from org roles and project memberships,
    decided by :mod:`iki_server.org.permissions`. Django's own permission
    and group tables are not used.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(max_length=254, unique=True, db_collation=CASE_INSENSITIVE)
    #: Full name as the identity provider reports it.
    name = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Inactive users can't sign in, and their sessions end at once."
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        db_table = "iki_user"
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email

    def get_full_name(self) -> str:
        return self.name or self.email

    def get_short_name(self) -> str:
        return self.name.split(" ")[0] if self.name else self.email


class UserSession(AbstractBaseSession):
    """A browser session, linked to its user so sessions can be listed and revoked."""

    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="sessions"
    )

    class Meta:
        db_table = "iki_session"

    @classmethod
    def get_session_store_class(cls) -> type:
        from iki_server.accounts.sessions import SessionStore

        return SessionStore


class TokenAuthMethod(models.TextChoices):
    CLIENT_SECRET_BASIC = "client_secret_basic", "HTTP Basic"
    CLIENT_SECRET_POST = "client_secret_post", "Form POST"


def new_provider_id() -> str:
    return f"sso-{uuid7().hex[-8:]}"


class IdentityProvider(models.Model):
    """An OpenID Connect identity provider, configured in the first-run wizard.

    Exactly one provider is enabled at a time. Each issuer gets its own
    ``provider_id`` (part of the redirect URI), so accounts from one issuer
    can never be matched to subject identifiers from another.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    provider_id = models.SlugField(max_length=32, unique=True, default=new_provider_id)
    display_name = models.CharField(max_length=100)
    issuer = models.URLField(max_length=500, unique=True)
    client_id = models.CharField(max_length=255)
    client_secret_encrypted = models.TextField()
    token_auth_method = models.CharField(
        max_length=32,
        choices=TokenAuthMethod.choices,
        default=TokenAuthMethod.CLIENT_SECRET_BASIC,
    )
    #: From the provider's discovery document. Sign-in forms post to Iki,
    #: which redirects here, so the CSP form-action must allow this origin.
    authorization_endpoint = models.URLField(max_length=500)
    is_enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "iki_identity_provider"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["is_enabled"],
                condition=Q(is_enabled=True),
                name="iki_one_enabled_identity_provider",
            )
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.issuer})"

    @property
    def client_secret(self) -> str:
        return crypto.decrypt(self.client_secret_encrypted)

    @client_secret.setter
    def client_secret(self, value: str) -> None:
        self.client_secret_encrypted = crypto.encrypt(value)

    @property
    def authorization_origin(self) -> str:
        parts = urlsplit(self.authorization_endpoint)
        return f"{parts.scheme}://{parts.netloc}"

    def to_social_app(self) -> Any:  # noqa: ANN401 - allauth's SocialApp, built per request
        """An unsaved allauth ``SocialApp`` for this provider.

        allauth normally stores client secrets in plain text in its own
        table. Building the app per request keeps the secret encrypted at rest.
        """
        from allauth.socialaccount.models import SocialApp

        return SocialApp(
            provider="openid_connect",
            provider_id=self.provider_id,
            name=self.display_name,
            client_id=self.client_id,
            secret=self.client_secret,
            settings={
                "server_url": self.issuer,
                "oauth_pkce_enabled": True,
                "token_auth_method": self.token_auth_method,
                "fetch_userinfo": True,
            },
        )
