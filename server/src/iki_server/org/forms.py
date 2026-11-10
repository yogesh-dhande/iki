from typing import Any, cast

from django import forms
from django.db.models import QuerySet

from iki_server.accounts.models import User
from iki_server.org.models import (
    ORG_ROLE_DESCRIPTIONS,
    PROJECT_ROLE_DESCRIPTIONS,
    OrgRole,
    ProjectRole,
    Team,
)


def _role_choices() -> list[tuple[int, str]]:
    return [
        (role.value, f"{role.label}: {PROJECT_ROLE_DESCRIPTIONS[role]}") for role in ProjectRole
    ]


class TeamForm(forms.Form):
    name = forms.CharField(label="Team name", max_length=100, strip=True)
    description = forms.CharField(
        label="Description",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What the team works on.",
    )


class ProjectForm(forms.Form):
    team = forms.ModelChoiceField(
        label="Team",
        queryset=Team.objects.none(),
        empty_label=None,
        help_text="The team’s compute limits apply to the project, and the team pays for its runs.",
    )
    name = forms.CharField(label="Project name", max_length=100, strip=True)
    description = forms.CharField(
        label="Description",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args: Any, teams: QuerySet[Team], **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        cast("forms.ModelChoiceField[Team]", self.fields["team"]).queryset = teams


class ProjectSettingsForm(forms.Form):
    name = forms.CharField(label="Project name", max_length=100, strip=True)
    description = forms.CharField(
        label="Description",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
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


class AddPersonForm(forms.Form):
    person = PersonField()
    role = forms.TypedChoiceField(
        label="Role",
        coerce=int,
        choices=_role_choices,
        initial=ProjectRole.VIEWER.value,
        widget=forms.RadioSelect,
    )


class AddTeamForm(forms.Form):
    team = forms.ModelChoiceField(label="Team", queryset=Team.objects.none(), empty_label=None)
    role = forms.TypedChoiceField(
        label="Role",
        coerce=int,
        choices=_role_choices,
        initial=ProjectRole.VIEWER.value,
        widget=forms.RadioSelect,
        help_text="Everyone in the team gets this role, including people who join it later.",
    )

    def __init__(self, *args: Any, teams: QuerySet[Team], **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        cast("forms.ModelChoiceField[Team]", self.fields["team"]).queryset = teams


class ChangeRoleForm(forms.Form):
    role = forms.TypedChoiceField(label="Role", coerce=int, choices=ProjectRole.choices)


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
