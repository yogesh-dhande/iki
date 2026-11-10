"""Pages about notifications. The inbox itself is the web app's (``/notifications/``)."""

from django.urls import URLPattern, path

from iki_server.notifications import pages

app_name = "notifications"

urlpatterns: list[URLPattern] = [
    path("account/", pages.account, name="account"),
    path("account/notifications/", pages.preferences, name="preferences"),
    path("settings/email/", pages.org_email, name="org_email"),
]
