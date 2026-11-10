from django.urls import URLPattern, path, re_path

from iki_server.scim import views

app_name = "scim"

# No trailing slashes: identity providers call /scim/v2/Users, not /Users/.
urlpatterns: list[URLPattern] = [
    path("ServiceProviderConfig", views.service_provider_config, name="service_provider_config"),
    path("ResourceTypes", views.resource_types, name="resource_types"),
    path("Schemas", views.schemas, name="schemas"),
    path("Users", views.users, name="users"),
    path("Users/<uuid:resource_id>", views.user, name="user"),
    path("Groups", views.groups, name="groups"),
    path("Groups/<uuid:resource_id>", views.group, name="group"),
    re_path(r"^.*$", views.not_found),
]
