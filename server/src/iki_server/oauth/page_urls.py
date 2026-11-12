"""Pages people use around OAuth: signing in devices, approving apps, their authorized apps."""

from django.urls import URLPattern, path

from iki_server.oauth import pages, views

app_name = "oauth"

urlpatterns: list[URLPattern] = [
    path("oauth/device/", views.device, name="device"),
    path("oauth/device/<int:grant_id>/", views.device_confirm, name="device_confirm"),
    path(
        "oauth/apps/<int:client_id>/request-approval/",
        views.request_approval,
        name="request_approval",
    ),
    path("settings/apps/", pages.org_apps, name="org_apps"),
    path("settings/apps/<int:client_id>/", pages.org_app, name="org_app"),
    path("account/apps/", pages.my_apps, name="my_apps"),
    path("account/apps/grants/<uuid:grant>/", pages.my_grant, name="my_grant"),
    path("account/apps/delegate/", pages.delegable, name="delegable"),
    path("account/apps/delegate/<int:client_id>/", pages.delegate, name="delegate"),
]
