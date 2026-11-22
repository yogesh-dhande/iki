from django.urls import URLPattern, path

from iki_server.scim import pages

app_name = "identity"

urlpatterns: list[URLPattern] = [
    path("admin/sign-in/", pages.sign_in_settings, name="sign_in_settings"),
    path("admin/sign-in/groups/<uuid:group_id>/", pages.group_link, name="group_link"),
]
