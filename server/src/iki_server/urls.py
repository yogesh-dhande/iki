"""Root URL configuration. API routers are added as their features land."""

from django.urls import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = []
