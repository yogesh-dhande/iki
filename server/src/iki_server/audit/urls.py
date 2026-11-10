from django.urls import URLPattern, path

from iki_server.audit import views

app_name = "audit"

urlpatterns: list[URLPattern] = [
    path("audit/", views.audit_log, name="audit_log"),
]
