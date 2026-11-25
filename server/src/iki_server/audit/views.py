"""The audit log page, for Auditors."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, time, tzinfo
from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import CharField, Q, QuerySet
from django.db.models.functions import Cast
from django.http import HttpRequest, HttpResponse, QueryDict, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from iki_server.api import csvs
from iki_server.api.schemas import agent_out
from iki_server.audit import details, log
from iki_server.audit.log import ACTIONS
from iki_server.audit.models import AuditEvent, Via
from iki_server.entities.models import Entity
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
    actor = forms.CharField(label="Actor email or ID", required=False, max_length=254)
    record_type = forms.CharField(
        label="Record type",
        required=False,
        max_length=255,
        help_text="An audit target such as project, or a qualified entity type.",
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
    if actor := data["actor"]:
        actor_id = _uuid(actor)
        events = (
            events.filter(actor_id=actor_id)
            if actor_id
            else events.filter(actor_label__iexact=actor)
        )
    if kind := data["record_type"]:
        records = Entity.objects.filter(type=kind).values_list(Cast("pk", CharField()), flat=True)
        events = events.filter(Q(target_type=kind) | Q(target_type="entity", target_id__in=records))
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
    export_query = query.urlencode()
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
                        details.describe(event.action, event.data),
                    )
                    for event in page
                ],
                "export_query": export_query,
                "older_query": query.urlencode() if more else "",
                "filtered": any(
                    v for k, v in request.GET.items() if k not in {"before", timezones.PARAMETER}
                ),
            },
        )


@login_required
@require_GET
def export(request: HttpRequest) -> HttpResponse | StreamingHttpResponse:
    permissions.require(request.user, Action.AUDIT_READ)
    form = FilterForm(request.GET, zone=timezones.viewer_zone(request))
    if not form.is_valid():
        return HttpResponse("Check the audit log filters.", status=400)
    through = timezone.now()
    events = _filtered(form).filter(occurred_at__lte=through).order_by("-occurred_at", "-id")
    name = "Organization audit log.csv"
    filters = {
        key: str(value.pk if isinstance(value, Project) else value)
        for key, value in form.cleaned_data.items()
        if value
    }
    event = log.record(
        "audit.exported",
        actor=request.user,
        data={"file": name, "filters": filters, "through": through.isoformat()},
    )

    def rows() -> Iterator[list[object]]:
        yield [
            "Event ID",
            "Time (UTC)",
            "Action",
            "Actor ID",
            "Actor",
            "Through",
            "Target type",
            "Target ID",
            "Target",
            "Project ID",
            "Project",
            "Team ID",
            "Team",
            "Request ID",
            "Client IP",
            "OAuth client",
            "Agent ID",
            "Client ID",
            "Grant",
            "MCP session",
            "Agent software",
            "Agent model",
            "Details",
        ]
        for row in events.exclude(pk=event.pk).iterator(chunk_size=500):
            yield [
                row.pk,
                row.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                row.action,
                row.actor_id,
                row.actor_label,
                row.via,
                row.target_type,
                row.target_id,
                row.target_label,
                row.project_id,
                row.project.name if row.project else "",
                row.team_id,
                row.team.name if row.team else "",
                row.request_id,
                row.client_ip,
                row.oauth_client,
                row.agent_id,
                row.agent.client_id if row.agent else "",
                row.agent.grant if row.agent else None,
                row.agent.session if row.agent else "",
                row.agent.software if row.agent else "",
                row.agent.model if row.agent else "",
                json.dumps(row.data, ensure_ascii=False, sort_keys=True),
            ]

    return csvs.streamed(request, csvs.chunks(rows()), name)
