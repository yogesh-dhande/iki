from datetime import date
from typing import Any, cast

from django import forms
from django.db.models import QuerySet
from django.utils import timezone
from oauth2_provider.forms import AllowForm

from iki_server.oauth.scopes import RESERVED, SCOPES, ordered
from iki_server.oauth.services import MAX_DELEGATION_DAYS
from iki_server.org.models import Project

ACCESS_CHOICES = [
    ("chosen", "Only the projects I choose"),
    ("all", "All my projects, including ones I join later"),
]


class ScopeChoiceField(forms.MultipleChoiceField):
    widget = forms.CheckboxSelectMultiple


class ChoiceMixin(forms.Form):
    """What the person allows: which of the requested scopes, and which projects."""

    granted = ScopeChoiceField(label="It may", required=True)
    access = forms.ChoiceField(
        label="In", choices=ACCESS_CHOICES, initial="chosen", widget=forms.RadioSelect
    )
    projects = forms.ModelMultipleChoiceField(
        label="Projects",
        queryset=Project.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def setup_choices(self, requested: list[str], projects: QuerySet[Project]) -> None:
        cast("ScopeChoiceField", self.fields["granted"]).choices = [
            (scope, SCOPES[scope]) for scope in ordered(requested)
        ]
        cast("forms.ModelMultipleChoiceField[Project]", self.fields["projects"]).queryset = projects

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("access") == "chosen" and not data.get("projects") and self.wants_access():
            self.add_error("projects", "Choose at least one project, or allow all your projects.")
        return data

    def wants_access(self) -> bool:
        return True


class ConsentForm(ChoiceMixin, AllowForm):
    def __init__(
        self,
        *args: Any,  # noqa: ANN401
        requested: list[str] | None = None,
        projects: QuerySet[Project] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        super().__init__(*args, **kwargs)
        self.setup_choices(
            requested or [], projects if projects is not None else Project.objects.none()
        )
        # Denying needs none of the choices.
        if self.data and not self.data.get("allow"):
            for name in ("granted", "access", "projects"):
                self.fields[name].required = False

    def wants_access(self) -> bool:
        return bool(self.cleaned_data.get("allow"))


class DeviceCodeForm(forms.Form):
    user_code = forms.CharField(
        label="Code",
        max_length=20,
        strip=True,
        help_text="The code your terminal shows.",
        widget=forms.TextInput(
            attrs={"autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false"}
        ),
    )

    def clean_user_code(self) -> str:
        return "".join(self.cleaned_data["user_code"].split()).replace("-", "").upper()


class DeviceConsentForm(ChoiceMixin):
    pass


class CredentialForm(forms.Form):
    name = forms.CharField(
        label="Name",
        max_length=100,
        required=False,
        help_text="Where it’s used, such as “LIMS server”.",
    )
    scopes = forms.MultipleChoiceField(
        label="Access",
        choices=[(s, f"{s}: {d}") for s, d in SCOPES.items() if s != "admin" and s not in RESERVED],
        widget=forms.CheckboxSelectMultiple,
        initial=["read"],
        help_text="It can still only do what the service account’s roles allow.",
    )
    acts_for_people = forms.BooleanField(
        label="Let it act for people who delegate to it",
        required=False,
        help_text="People can then let it act for them, within their own access and the "
        "service account’s, until a date they choose. Allow it only for a credential kept "
        "in a secrets manager.",
    )


class DelegationForm(ChoiceMixin):
    """What a person lets a service account's credential do for them, and until when."""

    until = forms.DateField(
        label="Until",
        help_text=f"At most {MAX_DELEGATION_DAYS} days away. It stops at the end of that day.",
    )

    def __init__(self, *args: Any, latest: date, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.latest = latest
        self.fields["until"].widget = forms.DateInput(
            attrs={
                "type": "date",
                "min": timezone.localdate().isoformat(),
                "max": latest.isoformat(),
            }
        )

    def clean_until(self) -> date:
        until: date = self.cleaned_data["until"]
        if until < timezone.localdate():
            raise forms.ValidationError("Choose today or a later day.")
        if until > self.latest:
            raise forms.ValidationError(f"Choose a day at most {MAX_DELEGATION_DAYS} days away.")
        return until
