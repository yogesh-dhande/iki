"""Root URL configuration."""

from django.urls import URLPattern, URLResolver, include, path

from iki_server.accounts import views as accounts
from iki_server.api.v1 import api as api_v1
from iki_server.web import views as web

urlpatterns: list[URLPattern | URLResolver] = [
    path("", web.home, name="home"),
    path("login/", accounts.login, name="login"),
    path("logout/", accounts.logout, name="logout"),
    path("signed-out/", accounts.signed_out, name="signed_out"),
    path("setup/", include("iki_server.setup.urls")),
    path("", include("iki_server.org.urls")),
    path("", include("iki_server.audit.urls")),
    path("", include("iki_server.scim.page_urls")),
    path("accounts/", include(accounts.allauth_urlpatterns)),
    path("scim/v2/", include("iki_server.scim.urls")),
    path("api/v1/", api_v1.urls),
    path("csp-report/", web.csp_report, name="csp_report"),
]
