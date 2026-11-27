from typing import TYPE_CHECKING, Any

from django import forms
from django.utils.functional import lazy

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


def once_they_have_an_account() -> str:
    """When someone new can be added: once they have an account. With provisioning on,
    only the identity provider makes accounts, and a first sign-in is refused (ADR 0026),
    as GitHub's managed users and Slack's provisioned members are."""
    from iki_server.scim.tokens import scim_enabled

    if scim_enabled():
        return "once your identity provider provisions them"
    return "once they’ve signed in to Iki for the first time"


def _person_help() -> str:
    return (
        f"People can be added {once_they_have_an_account()}. Add a service account by its address."
    )


def _registrar_help() -> str:
    return (
        "They curate its entries' facts and supersede mistaken entries. People can be "
        f"appointed {once_they_have_an_account()}."
    )


class PersonField(forms.EmailField):
    """An email address of someone who has an Iki account."""

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        kwargs.setdefault("label", "Email address")
        kwargs.setdefault("max_length", 254)
        # Worked out as the form is shown: provisioning can be turned on and off.
        kwargs.setdefault("help_text", lazy(_person_help, str)())
        super().__init__(**kwargs)
        self.widget.attrs.update({"autocomplete": "off", "spellcheck": "false"})

    def clean(self, value: Any) -> User:  # noqa: ANN401
        from iki_server.scim.tokens import scim_enabled

        email = super().clean(value)
        user = User.objects.filter(email=email).first()
        if user is None:
            if scim_enabled():
                raise forms.ValidationError(
                    "No one with that email address has an Iki account. Your identity "
                    "provider provisions them: assign Iki to them there, then add them once "
                    "it has."
                )
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
    person = PersonField(label="Registrar's email address", help_text=lazy(_registrar_help, str)())


def _curator_help() -> str:
    return (
        "They add the list's options, change them, reorder the list, and archive and "
        f"supersede options, each with a reason. People can be appointed "
        f"{once_they_have_an_account()}."
    )


def list_choices() -> list[tuple[str, str]]:
    """The installed lab lists, by their plural label."""
    from iki_server.entities import lists

    return sorted(
        ((name, t.label_plural) for name, t in lists.types().items()),
        key=lambda c: (c[1].casefold(), c[0]),
    )


class AppointListCuratorForm(forms.Form):
    """A lab list's curator (ADR 0270), appointed by an Org Admin, a list at a time."""

    list_type = forms.ChoiceField(label="List", choices=list_choices)
    person = PersonField(label="Curator's email address", help_text=lazy(_curator_help, str)())


class LegalHoldReleaseForm(forms.Form):
    """Why a hold is released: required, as `holds.release` requires it, and kept in the
    hold and its audit event."""

    reason = forms.CharField(
        label="Reason for releasing it",
        max_length=2000,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 3, "autofocus": True}),
        help_text="Why the matter no longer needs it, and who decided. It’s kept in the audit log.",
        error_messages={
            "required": "Say why the legal hold is being released.",
            "max_length": "Keep the release reason to 2,000 characters.",
        },
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
