from django.urls import URLPattern, path

from iki_server.org import views

app_name = "org"

urlpatterns: list[URLPattern] = [
    path("projects/", views.project_list, name="project_list"),
    path("projects/new/", views.project_new, name="project_new"),
    path("projects/<slug:slug>/", views.project_detail, name="project_detail"),
    path("projects/<slug:slug>/members/", views.project_members, name="project_members"),
    path(
        "projects/<slug:slug>/members/<uuid:membership_id>/",
        views.project_member,
        name="project_member",
    ),
    path("projects/<slug:slug>/join/", views.project_join, name="project_join"),
    path("projects/<slug:slug>/settings/", views.project_settings, name="project_settings"),
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
]
