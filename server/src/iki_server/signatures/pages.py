"""Admin → Electronic signatures: the organization's certification to the FDA (21 CFR 11.100(c)),
whose signing failed attempts locked (§11.300(d)), and the keys that sign (§11.70).

Org Admins record a certification and unlock people; Auditors read it all.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities.values import Invalid
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.signatures import services, signing_keys
from iki_server.signatures.models import Certification


class CertificationForm(forms.Form):
    submitted_on = forms.DateField(
        label="Sent on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="When the organization sent its letter to the FDA.",
    )
    reference = forms.CharField(
        label="Reference",
        max_length=500,
        strip=True,
        help_text="Where the letter is kept, or how it was sent: “QA-DOC-0042, sent to FDA ORA”.",
    )


def locked() -> list[User]:
    """Whose signing is locked now, most recently first."""
    ids = (
        AuditEvent.objects.filter(action="signature.locked", target_type="user")
        .order_by("-occurred_at")
        .values_list("target_id", flat=True)
    )
    seen: list[str] = list(dict.fromkeys(ids))
    people = {str(u.pk): u for u in User.objects.filter(pk__in=seen)}
    return [people[i] for i in seen if i in people and services.locked_out(people[i])]


@login_required
@require_http_methods(["GET", "POST"])
def signatures(request: HttpRequest) -> HttpResponse:
    me = request.user
    if not isinstance(me, User):
        raise PermissionDenied
    manage = permissions.can(me, Action.ORG_MANAGE)
    if not manage:
        permissions.require(me, Action.AUDIT_READ)
    action = request.POST.get("action")
    form = CertificationForm(request.POST if action == "certify" else None)
    form.fields["submitted_on"].widget.attrs["max"] = timezone.localdate().isoformat()
    if request.method == "POST":
        if not manage:
            raise PermissionDenied
        if action == "unlock":
            person = get_object_or_404(User, pk=request.POST.get("user"))
            try:
                services.unlock(me, person)
            except Refused as refusal:
                messages.error(request, str(refusal))
            else:
                messages.success(
                    request,
                    f"Unlocked {person.name or person.email}’s signing. They sign in again to "
                    "sign.",
                )
            return redirect("org:signatures")
        if form.is_valid():
            try:
                services.certify(
                    me, form.cleaned_data["submitted_on"], form.cleaned_data["reference"]
                )
            except Invalid as invalid:
                for name, said in invalid.errors.items():
                    for each in said:
                        form.add_error(name if name in form.fields else None, each)
            else:
                messages.success(
                    request, "Recorded the certification. People can sign from now on."
                )
                return redirect("org:signatures")
    return render(
        request,
        "signatures/settings.html",
        {
            "manage": manage,
            "form": form,
            "certification": services.certified(),
            "earlier": list(
                Certification.objects.select_related("recorded_by").order_by("-recorded_at")[1:20]
            ),
            "locked": locked(),
            "keys": signing_keys.own(),
            "deployment": services.deployment(),
        },
    )
