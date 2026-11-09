from django.urls import path

from iki_server.setup import views

app_name = "setup"

urlpatterns = [
    path("", views.start, name="start"),
    path("organization/", views.organization, name="organization"),
    path("identity-provider/", views.identity_provider, name="identity_provider"),
    path("verify/", views.verify, name="verify"),
    path("complete/", views.complete, name="complete"),
    path("done/", views.done, name="done"),
]
