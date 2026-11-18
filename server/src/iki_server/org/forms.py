from typing import TYPE_CHECKING, Any

from django import forms

from iki_server.accounts.models import User
from iki_server.org.models import (
    ORG_ROLE_DESCRIPTIONS,
    OrgRole,
    Project,
    Team,
)


class TeamForm(forms.Form):
    name = forms.CharField(label="Team name", max_length=100, strip=True)
    description = forms.CharField(
        label="Description",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What the team works on.",
    )


class PersonField(forms.EmailField):
    """An email address of someone who has signed in to Iki before."""

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        kwargs.setdefault("label", "Email address")
        kwargs.setdefault("max_length", 254)
        kwargs.setdefault(
            "help_text",
            "People can be added once they’ve signed in to Iki for the first time. Add a "
            "service account by its address.",
        )
        super().__init__(**kwargs)
        self.widget.attrs.update({"autocomplete": "off", "spellcheck": "false"})

    def clean(self, value: Any) -> User:  # noqa: ANN401
        email = super().clean(value)
        user = User.objects.filter(email=email).first()
        if user is None:
            raise forms.ValidationError(
                "No one with that email address has signed in to Iki yet. Ask them to sign in "
                "once, then add them."
            )
        if not user.is_active:
            raise forms.ValidationError("That person’s account is deactivated.")
        return user


class AddTeamMemberForm(forms.Form):
    person = PersonField()


class OrgRolesForm(forms.Form):
    roles = forms.TypedMultipleChoiceField(
        label="Org roles",
        coerce=OrgRole,
        required=False,
        choices=[(role.value, f"{role.label}: {ORG_ROLE_DESCRIPTIONS[role]}") for role in OrgRole],
        widget=forms.CheckboxSelectMultiple,
    )


class ServiceAccountForm(forms.Form):
    name = forms.CharField(
        label="Name",
        max_length=100,
        strip=True,
        help_text="What acts as it, such as “LIMS sync” or “Plate reader 3”.",
    )
    description = forms.CharField(
        label="Description",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Who looks after it and what it’s for, for whoever reviews access next.",
    )


class ServiceAccountRolesForm(forms.Form):
    roles = forms.TypedMultipleChoiceField(
        label="Org roles",
        coerce=OrgRole,
        required=False,
        choices=[
            (role.value, f"{role.label}: {ORG_ROLE_DESCRIPTIONS[role]}")
            for role in OrgRole
            if role != OrgRole.ORG_ADMIN
        ],
        widget=forms.CheckboxSelectMultiple,
        help_text="Service accounts can’t be Org Admins.",
    )


if TYPE_CHECKING:
    _ModelChoice = forms.ModelChoiceField[Project]
else:
    _ModelChoice = forms.ModelChoiceField


class _ProjectChoice(_ModelChoice):
    def label_from_instance(self, obj: Project) -> str:
        return f"{obj.team.name} / {obj.name}"


class RegistryForm(forms.Form):
    """A registry (ADR 0115): its name, and which teams' projects may be in it."""

    name = forms.CharField(
        label="Name",
        max_length=100,
        strip=True,
        help_text="Such as a client's or a partner program's name.",
    )
    description = forms.CharField(
        label="Description",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Whose identities it keeps apart, and why.",
    )
    all_teams = forms.BooleanField(
        label="Every team's projects may be in it",
        required=False,
    )
    teams = forms.ModelMultipleChoiceField(
        label="Teams",
        queryset=Team.objects.filter(archived_at__isnull=True).order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="The teams whose new projects may be in it, if not every team's.",
    )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if not cleaned.get("all_teams") and not cleaned.get("teams"):
            self.add_error("teams", "Choose the teams that use it, or let every team use it.")
        return cleaned


class AppointRegistrarForm(forms.Form):
    person = PersonField(
        help_text="They curate its entries' facts and supersede mistaken entries. People can "
        "be appointed once they've signed in to Iki for the first time."
    )


class LegalHoldForm(forms.Form):
    project = _ProjectChoice(
        label="Project",
        queryset=Project.objects.select_related("team").order_by("team__name", "name"),
        empty_label="Choose a project",
    )
    matter = forms.CharField(
        label="Matter",
        max_length=200,
        strip=True,
        help_text="What it’s for, such as a case, an inspection or a request’s reference.",
    )
    note = forms.CharField(
        label="Note",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Who asked for it and how long it may last, for whoever reviews it next.",
    )
