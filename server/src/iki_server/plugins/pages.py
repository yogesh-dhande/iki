"""Settings → Plugins: what's installed, and purging a detached plugin (ADR 0092)."""

from typing import Any

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from iki_server.entities import registry
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.org.views import _user, page, refusals
from iki_server.plugins import purge
from iki_server.plugins.models import PluginPurge, PluginState


class PurgeForm(forms.Form):
    reason = forms.CharField(
        label="Why",
        max_length=purge.MAX_REASON,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Such as the retention it was kept for, and who approved its deletion. It’s "
        "kept in the audit log.",
    )
    confirm = forms.CharField(label="Type the plugin’s name to confirm", max_length=100, strip=True)


def _installed() -> list[dict[str, Any]]:
    entries = {e["name"]: e for e in (registry.installed() or {}).get("plugins", ())}
    states = {s.name: s for s in PluginState.objects.all()}
    out = []
    for name, plugin in registry.plugins().items():
        entry = entries.get(name, {})
        out.append(
            {
                "name": name,
                "title": plugin.title,
                "version": plugin.version,
                "iki": (plugin.iki or "").strip(),
                "sha": entry.get("sha") or "",
                "git": entry.get("git") or "",
                "fetched_from": entry.get("fetched_from") or "",
                "state": entry.get("state", "active"),
                "changed_at": states[name].changed_at if name in states else None,
                "purge": purge.pending(name),
            }
        )
    return out


def _purged() -> list[dict[str, Any]]:
    """Plugins a deploy purged (ADR 0092): no longer installed, but kept on record."""
    done = {
        p.plugin: p
        for p in PluginPurge.objects.filter(done_at__isnull=False)
        .select_related("requested_by")
        .order_by("done_at")
    }
    return [
        {
            "name": s.name,
            "title": "",
            "version": s.version,
            "sha": "" if s.sha == registry.LOCAL else s.sha,
            "state": "purged",
            "changed_at": s.changed_at,
            "purge": None,
            "done": done.get(s.name),
        }
        for s in PluginState.objects.filter(state=PluginState.State.PURGED).order_by("name")
        if s.name not in registry.plugins()
    ]


@page("GET")
def plugins(request: HttpRequest) -> HttpResponse:
    permissions.require(_user(request), Action.ORG_MANAGE)
    return render(request, "plugins/plugins.html", {"plugins": _installed() + _purged()})


@page("GET", "POST")
@refusals
def plugin(request: HttpRequest, name: str) -> HttpResponse:
    me = _user(request)
    permissions.require(me, Action.ORG_MANAGE)
    found = next((p for p in _installed() if p["name"] == name), None)
    if found is None:
        gone = next((p for p in _purged() if p["name"] == name), None)
        if gone is None:
            raise Http404
        return render(
            request,
            "plugins/plugin.html",
            {
                "plugin": gone,
                "purged": gone["done"],
                "report": getattr(gone["done"], "report", None),
            },
        )
    action = request.POST.get("action") if request.method == "POST" else None
    form = PurgeForm(request.POST if action == "purge" else None)
    scheduled = purge.pending(name)
    if action == "cancel" and scheduled is not None:
        purge.cancel(me, scheduled)
        messages.success(request, f"Cancelled the purge of {name}. Nothing was deleted.")
        return redirect("org:plugin", name=name)
    if action == "purge" and form.is_valid():
        try:
            scheduled = purge.request(
                me, name, form.cleaned_data["reason"], form.cleaned_data["confirm"]
            )
        except Refused as refusal:
            form.add_error(refusal.field, str(refusal))
        else:
            messages.success(
                request,
                f"Scheduled the purge of {name} for {scheduled.run_after:%Y-%m-%d}. Until then, "
                "it can be cancelled here.",
            )
            return redirect("org:plugin", name=name)
    detached = found["state"] == "detached"
    return render(
        request,
        "plugins/plugin.html",
        {
            "plugin": found,
            "detached": detached,
            "report": scheduled.report if scheduled else (purge.report(name) if detached else None),
            "scheduled": scheduled,
            "why_not": None if scheduled else purge.why_not(name),
            "form": form,
            "wait_days": settings.IKI_PURGE_WAIT // 86400,
        },
    )
