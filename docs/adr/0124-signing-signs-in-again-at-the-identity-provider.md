# 0124. Signing signs in again at the identity provider

- Status: Accepted
- Date: 2026-11-18
- Refines: [0023](0023-sso-only-sign-in-and-setup-link-wizard.md),
  [0026](0026-saml-and-scim-provisioning.md)
- Accepted with [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  the signature it makes

## Context

21 CFR 11.200(a) wants each electronic signature to use at least two
distinct identification components, an ID and a password; a series of
signings in one continuous session may use only the password after the
first. Veeva Vault, Benchling and MasterControl ask for the password at each
signature. Iki keeps no passwords (ADR 0023): the organization's identity
provider signs people in, with whatever it requires (a password and a second
factor, a passkey). §11.300(d) wants attempts at unauthorized use detected
and reported at once to the organization's security unit.

A browser session isn't proof: it may be hours old, or someone else at an
unlocked screen. Asking Iki's own form for a password isn't possible, and
would be weaker than what the provider asks.

## Decision

**Every signature signs in again at the identity provider**, however
recently the person did, with both components each time: Iki doesn't use
§11.200's allowance for later signings in a session, since the provider asks
for the same either way.

- **OpenID Connect**: an authorization request with `prompt=login` and
  `max_age=0`, a fresh `state`, `nonce` and PKCE (S256) verifier, the
  person's email as `login_hint`, and `acr_values` if the deployment asks
  for an assurance level (`IKI_SIGNING_AUTHN_CONTEXTS`). The ID token is
  checked as sign-in checks one (signature against the provider's keys, an
  allowed algorithm, issuer, audience, `azp`, expiry), then its `nonce` must
  be this request's, its `sub` the person's own account at the provider,
  and its `auth_time` present and no earlier than when they started signing
  (allowing a minute of clock skew). A provider that ignored `prompt=login`
  gives an old `auth_time`, and the signature fails.
- **SAML**: an AuthnRequest with `ForceAuthn="true"` (and the requested
  context, if set). The response must answer it (`InResponseTo`), be signed
  as sign-in's are (ADR 0026), name the person, and its `AuthnInstant` must
  be no earlier than when they started signing.

Both answer at the URLs sign-in already uses, so nothing more is registered
at the provider: Iki hands a callback to signing when its `state` (or
`RelayState`) is one of this session's signings, and to sign-in otherwise.
Signing in again signs nobody in and leaves the session as it was. What the
provider said (protocol, issuer, subject, when, `amr` or the authentication
context) goes into the signed document.

**A signing is short and bound to its session.** Starting one (`POST
/entities/{ref}/signatures`, with `If-Match` on the revision read) records
the record, revision, change, which signature, the reason and the
acknowledgment, for ten minutes, bound to a hash of the browser session; the
response says where to send the browser. Its answer is accepted once. Only
the web app, with a person's session, starts one: a token is refused.

**Failures are counted, reported and lock signing (§11.300(d)).** An answer
that doesn't prove the person signed in again (someone else's account, a
wrong `nonce`, an old `auth_time`, a bad signature) fails the signing, is in
the audit log, and tells Org Admins at once. Three within fifteen minutes
lock the person's signing, end their sessions and revoke their tokens, and
tell them and Org Admins; an Org Admin unlocks it in Settings → Signatures,
once they know who it was. Cancelling at the provider, or its own refusal to
sign someone in, fails the signing without counting.

## Consequences

- Each signature costs a round trip to the identity provider, as a password
  prompt costs one in other systems; providers keep the person signed in
  there, so it's often one sign-in screen.
- A provider must give `auth_time` (OIDC) or honour `ForceAuthn` (SAML).
  Keycloak, Okta and Entra ID do; one that doesn't can't be used for
  signing, and the signature says why.
- The organization's provider policy decides the two components; Iki records
  what the provider reports and can require an assurance level, but doesn't
  second-guess it.
