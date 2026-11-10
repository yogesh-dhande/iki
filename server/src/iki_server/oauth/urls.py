"""OAuth endpoints, under django-oauth-toolkit's URL names so its views and
metadata can reverse them. Mounted at the site root: the endpoints live under
``/oauth/`` and the metadata under ``/.well-known/`` (RFC 8414 §3, RFC 9728 §3)."""

from django.urls import URLPattern, path
from oauth2_provider import views as dot

from iki_server.oauth import views

app_name = "oauth2_provider"

urlpatterns: list[URLPattern] = [
    path("oauth/authorize/", views.AuthorizeView.as_view(), name="authorize"),
    path("oauth/token/", views.TokenView.as_view(), name="token"),
    path("oauth/revoke/", dot.RevokeTokenView.as_view(), name="revoke-token"),
    path(
        "oauth/device-authorization/",
        views.DeviceAuthorizationView.as_view(),
        name="device-authorization",
    ),
    path("oauth/register/", views.RegisterView.as_view(), name="dcr-register"),
    path(
        "oauth/register/<str:client_id>/",
        views.RegistrationManagementView.as_view(),
        name="dcr-register-management",
    ),
    path(
        ".well-known/oauth-authorization-server",
        views.ServerMetadataView.as_view(),
        name="oauth-server-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource",
        dot.OAuthProtectedResourceMetadataView.as_view(),
        name="oauth-resource-metadata",
    ),
]
