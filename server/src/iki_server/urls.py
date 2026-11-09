"""Root URL configuration. API routers are added as their features land."""

from django.urls import URLPattern, URLResolver, include, path

from iki_server.accounts import views as accounts
from iki_server.web import views as web

urlpatterns: list[URLPattern | URLResolver] = [
    path("", web.home, name="home"),
    path("login/", accounts.login, name="login"),
    path("logout/", accounts.logout, name="logout"),
    path("signed-out/", accounts.signed_out, name="signed_out"),
    path("setup/", include("iki_server.setup.urls")),
    path("accounts/", include(accounts.allauth_urlpatterns)),
    path("csp-report/", web.csp_report, name="csp_report"),
]
