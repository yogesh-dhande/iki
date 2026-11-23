from django.urls import URLPattern, path

from iki_server.org import views
from iki_server.plugins import pages as plugin_pages
from iki_server.signatures import pages as signature_pages
from iki_server.web.views import app

app_name = "org"

urlpatterns: list[URLPattern] = [
    # The web app's pages. The names stay, for links from server-rendered pages.
    path("projects/", app, name="project_list"),
    path("projects/new/", app, name="project_new"),
    path("projects/<slug:slug>/", app, name="project_detail"),
    path("projects/<slug:slug>/members/", app, name="project_members"),
    path("projects/<slug:slug>/entities/", app, name="project_entities"),
    path("projects/<slug:slug>/entities/new/", app, name="project_entity_new"),
    # Importing a spreadsheet (ADR 0178), and an import at its step.
    path("projects/<slug:slug>/entities/import/", app, name="project_sheet_import"),
    path(
        "projects/<slug:slug>/entities/import/<uuid:import_id>/",
        app,
        name="project_sheet_import_detail",
    ),
    path("projects/<slug:slug>/requests/", app, name="project_requests"),
    path("projects/<slug:slug>/runs/", app, name="project_runs"),
    path("projects/<slug:slug>/runs/new/", app, name="project_run_new"),
    path("projects/<slug:slug>/files/", app, name="project_files"),
    # Automation's three (ADR 0176).
    path("projects/<slug:slug>/automation/", app, name="project_automation"),
    path("projects/<slug:slug>/automation/workflows/", app, name="project_workflows"),
    path("projects/<slug:slug>/automation/workflows/new/", app, name="project_workflow_new"),
    path("projects/<slug:slug>/automation/triggers/", app, name="project_triggers"),
    path("projects/<slug:slug>/automation/webhooks/", app, name="project_webhooks"),
    path(
        "projects/<slug:slug>/automation/webhooks/<uuid:webhook_id>/",
        app,
        name="project_webhook",
    ),
    path("projects/<slug:slug>/settings/", app, name="project_settings"),
    path("projects/<slug:slug>/settings/starters/", app, name="project_starters"),
    path("projects/<slug:slug>/imports/<uuid:import_id>/", app, name="project_import"),
    path("projects/<slug:slug>/apps/drafts/<uuid:draft>/", app, name="project_app_draft"),
    path("teams/", views.team_list, name="team_list"),
    path("teams/<slug:slug>/", views.team_detail, name="team_detail"),
    path(
        "teams/<slug:slug>/members/<uuid:user_id>/remove/",
        views.team_member_remove,
        name="team_member_remove",
    ),
    path("teams/<slug:slug>/settings/", views.team_settings, name="team_settings"),
    path("admin/legal-holds/", views.legal_holds, name="legal_holds"),
    path("admin/signatures/", signature_pages.signatures, name="signatures"),
    path("admin/registries/", views.registry_list, name="registries"),
    path("admin/registries/<uuid:registry_id>/", views.registry_detail, name="registry"),
    path("admin/plugins/", plugin_pages.plugins, name="plugins"),
    path("admin/plugins/<plugin:name>/", plugin_pages.plugin, name="plugin"),
    path("admin/people/", views.people, name="people"),
    path("admin/people/<uuid:user_id>/", views.person, name="person"),
    path("admin/service-accounts/", views.service_accounts, name="service_accounts"),
    path("admin/service-accounts/<uuid:user_id>/", views.service_account, name="service_account"),
]
