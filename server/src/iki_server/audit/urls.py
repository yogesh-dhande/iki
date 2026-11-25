from django.urls import URLPattern, path

from iki_server.audit import views

app_name = "audit"

urlpatterns: list[URLPattern] = [
    path("admin/audit/export.csv", views.export, name="export"),
    path("admin/audit/", views.audit_log, name="audit_log"),
]
