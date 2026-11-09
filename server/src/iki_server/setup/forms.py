from typing import Any

from django import forms


class TokenForm(forms.Form):
    token = forms.CharField(
        label="Setup code",
        max_length=200,
        strip=True,
        widget=forms.TextInput(
            attrs={"autocomplete": "off", "spellcheck": "false", "autofocus": True}
        ),
        help_text="The code after # in the link from iki-server setup_link.",
    )


class OrganizationForm(forms.Form):
    organization_name = forms.CharField(
        label="Organization name",
        max_length=100,
        strip=True,
        widget=forms.TextInput(attrs={"autocomplete": "organization", "autofocus": True}),
        help_text="Shown on the sign-in page and in page titles.",
    )


class IdentityProviderForm(forms.Form):
    display_name = forms.CharField(
        label="Provider name",
        max_length=100,
        strip=True,
        help_text=(
            "The sign-in button reads “Continue with …”, "
            "for example “Okta” or “Microsoft Entra ID”."
        ),
    )
    issuer = forms.URLField(
        label="Issuer URL",
        max_length=500,
        assume_scheme="https",
        widget=forms.URLInput(
            attrs={"spellcheck": "false", "placeholder": "https://login.example.com/"}
        ),
        help_text=(
            "Exactly as your provider reports it. "
            "Iki reads /.well-known/openid-configuration under it."
        ),
    )
    client_id = forms.CharField(
        label="Client ID",
        max_length=255,
        strip=True,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    client_secret = forms.CharField(
        label="Client secret",
        max_length=2000,
        strip=True,
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
        help_text="Stored encrypted.",
    )

    def __init__(self, *args: Any, has_secret: bool = False, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.has_secret = has_secret
        if has_secret:
            self.fields[
                "client_secret"
            ].help_text = "Stored encrypted. Leave empty to keep the current secret."

    def clean_issuer(self) -> str:
        return str(self.cleaned_data["issuer"]).strip()

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if not self.has_secret and not data.get("client_secret"):
            self.add_error("client_secret", "Enter the client secret.")
        return data
