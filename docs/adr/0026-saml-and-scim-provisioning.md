# 0026. SAML sign-in, and SCIM provisioning that decides who has access

- Status: Accepted
- Date: 2026-11-09
- Refines: [0023](0023-sso-only-sign-in-and-setup-link-wizard.md),
  [0024](0024-project-access-and-org-admin-boundaries.md),
  [0025](0025-append-only-audit-log-and-database-roles.md)

## Context

Iki's customers are mostly pharma and life-science companies. Their IT
teams run Microsoft Entra ID or Okta. Many connect enterprise apps over
SAML, and they expect access to follow the identity provider: joining a
group grants it, and leaving the company removes it everywhere, at once.
Their quality and audit teams also expect access reviews to have one source
of truth, and a record that can be attributed to whoever changed access.

Where GitHub Enterprise, Slack Enterprise Grid or similar products have a
settled convention, Iki follows it.

## Decision

**SAML 2.0 is a second sign-in protocol**, chosen in the setup wizard next
to OpenID Connect. One provider is enabled at a time, as before.

- **Metadata:** the wizard reads the provider's metadata once, from a URL or
  pasted XML. It stores the entity ID, the HTTP-Redirect single sign-on URL
  and the signing certificate.
- **Pinned certificate:** the certificate changes only when someone runs the
  wizard again (`setup_link --recover`), never because a URL started serving
  different metadata. Iki refuses expired certificates and RSA keys under
  2048 bits. The sign-in settings page warns 30 days before the certificate
  expires.
- **Strict checks:**
  - assertions must be signed with that certificate; a signed response
    alone isn't enough;
  - SHA-1 is refused;
  - audience, recipient, destination and validity window are checked;
  - XML is parsed without DTDs.
- **No IdP-initiated sign-in:** a response must answer a sign-in Iki started,
  in the same browser, so a captured response can't be replayed or planted
  in another session. An app-launcher tile points at Iki's sign-in page
  instead.
- **Fixed URLs:** every SAML provider shares the same URLs under
  `/accounts/saml/sso/`, so the ACS URL and service-provider entity ID
  entered at the identity provider survive reconfiguration. Each provider
  still has its own provider ID, so subjects from different providers never
  collide.
- **No single logout,** as with OIDC (ADR 0023).
- **Library:** python3-saml, through allauth.
- **Rate limit:** posting responses to the ACS is rate limited per client
  address, because checking signatures costs CPU.

**SCIM 2.0 provisioning, at `/scim/v2/`.**

- **What it supports:** Users and Groups, with the filters, PATCH styles and
  quirks Entra ID and Okta use. Attributes Iki doesn't store are ignored, so
  the providers' default mappings work unchanged.
- **The token:** an Org Admin turns provisioning on from the sign-in settings
  page.
  - That creates a bearer token, shown once; only its SHA-256 is stored.
  - One token is live at a time. Replacing it revokes the old one at once,
    and revoking it turns provisioning off.
  - Requests without a valid token are rate limited.
- **Audit:** every change is audited through the SCIM channel, naming the
  token that made it.
- **Users:** creating someone who already has an Iki account takes that
  account over, keeping their roles and history.
  - Deactivation ends their sessions at once. Later work adds API tokens,
    OAuth grants and client workers to the same revocation.
  - A SCIM DELETE deactivates the account and stops SCIM managing it. The
    account itself stays, so the audit log and project history keep pointing
    at a real person.

**While provisioning is on, only provisioned people get accounts.**

- A first sign-in from anyone the identity provider hasn't provisioned is
  refused and recorded. GitHub Enterprise and Slack work the same way.
- Accounts created before SCIM was turned on keep working. The settings page
  counts them, so admins can assign them in the identity provider.
- Break-glass recovery isn't blocked: the person completing it proves
  cluster access.

**A provisioned person's first sign-in links to their account.** This is
the one exception to ADR 0023's "never link by email".

- The link is made only when an account was provisioned over SCIM and has
  no identity yet from the enabled provider. It matches by email or SCIM
  `userName`.
- After that, the subject identifier decides, as for everyone else.
- This is safe because the same organization-controlled provider vouches for
  both the provisioned account and the sign-in. It is also what lets
  provisioned people survive a switch of identity provider.

**The identity provider alone activates and deactivates provisioned
people.** Iki's People page doesn't offer it for them, so the two never
disagree, and an access review needs only the identity provider. Org Admins
can still sign anyone out everywhere at once, for example during an
incident.

**Groups decide team membership once an Org Admin links them** (decided
with the product owner).

- Pushed groups change nothing until an Org Admin links one to an existing
  team, or to a new team named after it. The page lists who will join and
  leave before they confirm.
- From then on, the team's members are exactly the members of its linked
  groups (several groups may feed one team). Adding or removing members by
  hand in Iki is refused, as with GitHub team sync.
- Unlinking, or deleting the group at the provider, leaves the team's
  members as they are, and the team is managed by hand again.
- Groups don't grant org roles. Org Admins, Compute Admins and Auditors are
  still assigned in Iki, by an Org Admin, and audited.

## Consequences

- Positive:
  - Offboarding in Entra ID or Okta reaches Iki within the provider's sync,
    and ends sessions at once.
  - Team access follows the groups the IT team already maintains, and every
    resulting change is audited with its cause.
  - SAML responses can't be forged, downgraded, replayed or planted in
    another session.
- Negative / trade-offs:
  - Renewing a SAML certificate needs a recovery link and cluster access.
    That gives the change a controlled path and an audit record, but it is
    a step the IT team must remember. The warning on the settings page is
    there for that.
  - IdP-initiated sign-in (app-launcher tiles that post straight to Iki)
    isn't supported; tiles must point at Iki's sign-in page.
  - While provisioning is on, someone the IT team forgot to assign can't get
    in until they are assigned.
  - One live SCIM token means a brief gap when rotating it. Identity
    providers retry failed requests.
  - The identity provider can deactivate the last active Org Admin, because
    offboarding must always work. `setup_link --recover` restores an
    administrator through cluster access.

## Alternatives considered

- **Every pushed group becomes a team:** less setup, but identity providers
  push many unrelated groups, and Iki's team list would mirror them.
- **Groups only add members:** removing someone from a group wouldn't remove
  their access, which defeats offboarding through groups.
- **Allow sign-ins from anyone the provider authenticates, alongside SCIM:**
  people outside SCIM would only be offboarded when their sessions expire.
- **Refreshing SAML metadata from its URL automatically:** certificates would
  rotate without anyone noticing, but whoever can change what that URL
  serves could change Iki's trust anchor.
- **Accepting IdP-initiated SAML:** convenient for app launchers, but opens
  login CSRF and replay, which pharma security reviews flag.
- **django-scim2:** built around Django's own user and group tables, which
  Iki doesn't use (ADR 0024), and without Entra's PATCH quirks.
