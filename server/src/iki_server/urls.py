"""Root URL configuration."""

from django.urls import URLPattern, URLResolver, include, path

from iki_server.accounts import views as accounts
from iki_server.api import worker
from iki_server.api.v1 import api as api_v1
from iki_server.mcp import views as mcp
from iki_server.realtime import views as realtime
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
    # Before the API's own routes: an async streaming view, outside django-ninja.
    path("api/v1/events", realtime.events, name="events"),
    path("api/v1/runs/<str:ref>/events", realtime.run_events, name="run-events"),
    path("api/v1/batches/<str:ref>/events", realtime.batch_events, name="batch-events"),
    path("api/v1/compute/setups/<str:ref>/events", realtime.setup_events, name="setup-events"),
    path("api/v1/", api_v1.urls),
    # AI agents' (ADR 0013): MCP over Streamable HTTP.
    path("mcp", mcp.endpoint, name="mcp"),
    # Workers' (ADR 0043). Leasing waits for a job, and watching one waits for it to be
    # cancelled, so they're async views outside django-ninja.
    path("worker/v1/lease", worker.lease, name="worker_lease"),
    path("worker/v1/jobs/<uuid:job_id>/watch", worker.watch, name="worker_watch"),
    path("worker/v1/", worker.api.urls),
    path("", include("iki_server.oauth.urls")),
    path("", include("iki_server.oauth.page_urls")),
    path("", include("iki_server.runs.page_urls")),
    path("", include("iki_server.notifications.urls")),
    path("notifications/", web.app, name="notifications"),
    # The web app's: the organization's webhooks (project ones are in org.urls).
    path("settings/webhooks/", web.app, name="webhook_settings"),
    path("compute/volumes/", web.app, name="compute_volumes"),
    path("settings/webhooks/<uuid:webhook_id>/", web.app, name="webhook_setting"),
    # Entities and their types (a project's entities are in org.urls).
    path("types/", web.app, name="entity_types"),
    path("types/<str:name>/", web.app, name="entity_type"),
    path("entities/<str:ref>/", web.app, name="entity"),
    path("entities/<str:ref>/edit/", web.app, name="entity_edit"),
    path("entities/<str:ref>/history/", web.app, name="entity_history"),
    # Runs (a project's runs are in org.urls).
    path("runs/<str:ref>/", web.app, name="run"),
    path("batches/<str:ref>/", web.app, name="batch"),
    path("csp-report/", web.csp_report, name="csp_report"),
]
