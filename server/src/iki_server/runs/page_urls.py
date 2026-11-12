"""Pages about the workers someone runs on their own computers."""

from django.urls import URLPattern, path

from iki_server.runs import pages

app_name = "runs"

urlpatterns: list[URLPattern] = [
    path("account/workers/", pages.my_workers, name="my_workers"),
]
