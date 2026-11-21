"""The audit log page, for Auditors."""

import uuid
from datetime import datetime, time, tzinfo
from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from iki_server.api.schemas import agent_out
from iki_server.audit.log import ACTIONS
from iki_server.audit.models import AuditEvent, Via
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.web import timezones

PAGE_SIZE = 50


class FilterForm(forms.Form):
    action = forms.ChoiceField(
        label="What",
        required=False,
        choices=[("", "Everything"), *sorted(ACTIONS.items(), key=lambda item: item[1])],
    )
    person = forms.CharField(
        label="Who",
        required=False,
        max_length=254,
        help_text="Who did it or who it was about: an email address, or part of one.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    project = forms.ModelChoiceField(
        label="Project", required=False, queryset=Project.objects.order_by("name")
    )
    via = forms.ChoiceField(label="Through", required=False, choices=[("", "Any"), *Via.choices])
    agent = forms.CharField(
        label="Agent",
        required=False,
        max_length=255,
        help_text="An app’s client ID: what it did for anyone.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    grant = forms.UUIDField(
        label="Grant",
        required=False,
        help_text="One person’s grant to an app: everything it did with it.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    since = forms.DateField(
        label="From", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    until = forms.DateField(
        label="Until", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    #: The zone the dates are days in, so a link finds the same events for anyone.
    tz = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, data: QueryDict | None, *, zone: tzinfo) -> None:
        name = timezones.zone_name(zone)
        if data is not None:
            # The zone the page used, whatever the address asked for.
            data = data.copy()
            data["tz"] = name
        super().__init__(data, initial={"tz": name})
        self.zone = zone
        self.fields["since"].help_text = f"From the start of that day, in {name}."
        self.fields["until"].help_text = f"To the end of that day, in {name}."

def _filtered(form: FilterForm) -> QuerySet[AuditEvent]:
    events = AuditEvent.objects.select_related("project", "team", "agent")
    if not form.is_valid():
        return events.none()
    data: dict[str, Any] = form.cleaned_data
    if data["action"]:
        events = events.filter(action=data["action"])
    if data["person"]:
        events = events.filter(
            Q(actor_label__icontains=data["person"]) | Q(target_label__icontains=data["person"])
        )
    if data["project"]:
        events = events.filter(project=data["project"])
    if data["via"]:
        events = events.filter(via=data["via"])
    if data["agent"]:
        client_id = data["agent"].strip()
        # Older events name the client only in their label (ADR 0072).
        events = events.filter(
            Q(agent__client_id=client_id)
            | Q(agent=None, oauth_client__endswith=f"({client_id[:40]})")
        )
    if data["grant"]:
        events = events.filter(agent__grant=data["grant"])
    zone = form.zone
    if data["since"]:
        events = events.filter(occurred_at__gte=datetime.combine(data["since"], time.min, zone))
    if data["until"]:
        events = events.filter(occurred_at__lte=datetime.combine(data["until"], time.max, zone))
    return events


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@login_required
@require_GET
def audit_log(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, Action.AUDIT_READ)
    zone = timezones.viewer_zone(request)
    form = FilterForm(request.GET or None, zone=zone)
    events = (
        _filtered(form)
        if request.GET
        else AuditEvent.objects.select_related("project", "team", "agent")
    )
    # Keyset pagination: stable while new events arrive.
    before = _uuid(request.GET.get("before"))
    if before:
        anchor = AuditEvent.objects.filter(pk=before).values("occurred_at", "id").first()
        if anchor:
            events = events.filter(
                Q(occurred_at__lt=anchor["occurred_at"])
                | Q(occurred_at=anchor["occurred_at"], id__lt=anchor["id"])
            )
    page = list(events.order_by("-occurred_at", "-id")[: PAGE_SIZE + 1])
    more = len(page) > PAGE_SIZE
    page = page[:PAGE_SIZE]
    query = request.GET.copy()
    query.pop("before", None)
    if more:
        query["before"] = str(page[-1].pk)
    with timezone.override(zone):
        return render(
            request,
            "audit/log.html",
            {
                "form": form,
                "zone": timezones.zone_name(zone),
                "events": [
                    (
                        event,
                        ACTIONS.get(event.action, event.action),
                        agent_out(event.agent, event.oauth_client),
                    )
                    for event in page
                ],
                "older_query": query.urlencode() if more else "",
                "filtered": any(
                    v for k, v in request.GET.items() if k not in {"before", timezones.PARAMETER}
                ),
            },
        )
