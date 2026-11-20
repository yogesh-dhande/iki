# 0151. A first sign-in links by what the identity provider vouches for

- Status: Accepted
- Date: 2026-11-19
- Amends: [0026](0026-saml-and-scim-provisioning.md)
- Refines: [0023](0023-sso-only-sign-in-and-setup-link-wizard.md),
  [0096](0096-bundles-carry-entities-between-deployments-with-their-history.md)

## Context

Iki keys an account by the identity provider's subject identifier, and
never links a sign-in to an account by email (ADR 0023). Two exceptions
exist, for a person's first sign-in:

- **A provisioned account** (ADR 0026). SCIM created it before the person
  ever signed in, so nothing links it to a subject yet. ADR 0026 matched it
  by email or SCIM `userName`.
- **A placeholder** (ADR 0096). A bundle's records named someone by email,
  and the first person to sign in with that email takes the placeholder
  over.

ADR 0026 trusted the email because the same provider vouches for the
provisioned account and the sign-in. That holds only if the provider
checked that the person owns the address. Some providers let people set or
change their own email without proving it: Keycloak with profile editing,
Auth0 database sign-ups, Okta self-service registration. Their tokens say
so, with `email_verified: false`. Someone who signs in first with a new
hire's address would get the new hire's account, teams and projects, and
their records would be attributed to the new hire.

The providers Iki's customers use have stable IDs for this:

- **Okta** sends its user ID as the SCIM `externalId`, and as the OpenID
  Connect `sub`.
- **Microsoft Entra ID** gives each app a different `sub`. The person's
  object ID, `oid`, is the same everywhere in the tenant (`tid`).
  Microsoft says to identify people by `oid` and `tid`, not by email. Its
  tokens carry no `email_verified`. The optional `xms_edov` claim says
  that the email's domain is verified as its owner's, not that the
  mailbox is this person's: an address IT has reassigned still carries it.
  Entra's SCIM sends `objectId` as `externalId` when mapped so. GitHub
  Enterprise Managed Users links Entra sign-ins the same way.
- **SAML** carries no `email_verified`, but its subject (the NameID) is the
  provider's own identifier. Entra and Okta send the same sign-in name as
  the SCIM `userName` by default. GitHub links SAML identities to SCIM
  identities by that match.

## Decision

**A first sign-in links to a provisioned account by what the provider
vouches for, strongest first.** The first rule that names exactly one
provisioned account decides:

1. **The person's immutable ID equals the SCIM `externalId`.** The ID is
   OpenID Connect's `sub`; Entra's `oid`, when the token's `tid` is the
   tenant the provider's issuer names; or the SAML subject, or Entra's
   `objectidentifier` attribute in the tenant its entity ID names.
2. **The SAML subject equals the SCIM `userName`.**
3. **An email address the provider verified equals the account's email or
   SCIM `userName`.** OpenID Connect's `email_verified` must be true (the
   string `"true"` counts too). Entra's `xms_edov` doesn't count. For SAML,
   an `email_verified` attribute must be true, or the subject must be the
   address itself.

**Names link only an active account.** Rules 2 and 3 match a sign-in name or
an address, which an organization can give to someone else once a person
leaves. A deactivated account is linked only by rule 1; to anyone else its
address is in use.

An address the provider doesn't say it verified links nobody, whether the
claim is false or missing. The sign-in is refused, and the refusal is
audited as `email_unverified`. The page tells the person to ask their IT
team to send their ID at the provider as `externalId` (for Entra, to map
`objectId` to it), or to have the provider verify the address. That is the
administrator's reconciliation: the provider's administrator says, through
SCIM, which of its people the account is. When two accounts answer to the same rule, the provider's
data is ambiguous, and nobody is linked. `user.identity_linked` records
which rule matched (`matched_by`).

**A placeholder is claimed only by an address the provider verified**, by
rule 3. Its records are attributed to whoever claims it, so the bar is the
same.

**Setup and break-glass recovery vouch for themselves.** In a session
holding the setup grant, the address counts as verified: whoever completes
it proved cluster access (ADR 0023). Recovery after a change of identity
provider must reach the account the old provider provisioned.

**After the first sign-in, nothing changes:** the subject identifier
decides, as for everyone else.

## Consequences

- Positive:
  - An address someone typed in at the provider no longer reaches anyone
    else's account or records.
  - Okta works with its defaults: its `externalId` is the `sub`, and its
    tokens say whether the email is verified.
  - Entra sign-ins link by object ID, as Microsoft and GitHub recommend,
    so a renamed or reused address can't redirect a first sign-in.
  - The audit log says how each account was linked.
- Negative / trade-offs:
  - **Entra over OpenID Connect needs SCIM to map `objectId` to
    `externalId`** for a person's first sign-in to link, as GitHub
    Enterprise Managed Users requires. Entra's default SCIM mapping sends
    `mailNickname`, and its tokens verify no one's mailbox. Without the
    mapping, a provisioned person is refused with an explanation.
  - Without SCIM, an Entra sign-in over OpenID Connect claims no
    placeholder by its address. Turning SCIM on claims it: provisioning
    the person does (ADR 0096).
  - A SAML provider whose subject is opaque, and which sends neither the
    subject as `externalId` nor an `email_verified` attribute, can't link
    a provisioned account or claim a placeholder by its email attribute.
  - A placeholder from a provider that verifies no addresses stays
    unclaimed until the provider does.

## Alternatives considered

- **Refuse only an explicit `email_verified: false`.** Entra and many SAML
  providers send nothing, so a missing claim would still link by an
  address nobody checked.
- **Trust the email of a SAML assertion**, since the assertion is signed.
  The signature proves who sent it, not that the person owns the address.
- **Link by `preferred_username` or Entra's UPN.** Microsoft documents both
  as mutable and unsuitable for authorization.
- **Require `externalId` for every link.** Okta's tokens say whether an
  address is verified, and SAML providers send the subject as `userName`, so
  their defaults would stop working for no gain.
- **Trust Entra's `xms_edov`.** It verifies the domain's owner, not the
  person's mailbox, so an address IT reassigned to someone else would link
  them to the previous holder's account, teams and records.
