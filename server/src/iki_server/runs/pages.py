"""The workers someone signed in on their own computers (ADR 0058)."""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from iki_server.org.views import _user, page, refusals
from iki_server.runs import workers
from iki_server.runs.models import Worker
from iki_server.runs.queue import HELD


@page("GET", "POST")
@refusals
def my_workers(request: HttpRequest) -> HttpResponse:
    me = _user(request)
    mine = (
        Worker.objects.filter(owner=me, tier="client", revoked_at__isnull=True)
        .annotate(held_jobs=Count("jobs", filter=Q(jobs__state__in=HELD)))
        .order_by("name", "registered_at")
    )
    if request.method == "POST" and request.POST.get("action") == "revoke":
        worker = get_object_or_404(mine, pk=request.POST.get("worker"))
        workers.revoke_worker(worker, by=me)
        messages.success(
            request,
            f"Revoked {worker.name}. It’s signed out, and any run it had is retried "
            "if its action allows.",
        )
        return redirect(request.path)
    now = timezone.now()
    return render(
        request,
        "runs/my_workers.html",
        {
            "workers": [(w, now - w.last_seen_at <= workers.OFFLINE_AFTER) for w in mine],
            "server_url": request.build_absolute_uri("/").rstrip("/"),
        },
    )
