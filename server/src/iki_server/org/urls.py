from django.urls import URLPattern, path

from iki_server.org import views
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
    path("projects/<slug:slug>/settings/", app, name="project_settings"),
    path("projects/<slug:slug>/webhooks/", app, name="project_webhooks"),
    path("projects/<slug:slug>/webhooks/<uuid:webhook_id>/", app, name="project_webhook"),
    path("teams/", views.team_list, name="team_list"),
    path("teams/<slug:slug>/", views.team_detail, name="team_detail"),
    path(
        "teams/<slug:slug>/members/<uuid:user_id>/remove/",
        views.team_member_remove,
        name="team_member_remove",
    ),
    path("teams/<slug:slug>/settings/", views.team_settings, name="team_settings"),
    path("people/", views.people, name="people"),
    path("people/<uuid:user_id>/", views.person, name="person"),
    path("service-accounts/", views.service_accounts, name="service_accounts"),
    path("service-accounts/<uuid:user_id>/", views.service_account, name="service_account"),
]
