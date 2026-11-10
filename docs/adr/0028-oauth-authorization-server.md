# 0028. The OAuth 2.1 authorization server

- Status: Accepted
- Date: 2026-11-10
- Refines: [0013](0013-agents-via-mcp-oauth-approvals.md),
  [0027](0027-rest-api-conventions-and-service-accounts.md)

## Context

The SDK, the CLI, MCP clients such as Claude Code, notebooks and customers'
own tools all call `/api/v1` for a person, and service accounts call it for
automation. Each needs a token that does less than the person could, can be
taken back at once, and leaves an audit trail naming the client.

Pharma and life-science security teams review every app that touches their
data. They expect an allowlist of approved apps, as GitHub organizations
and Slack workspaces have; access that ends when someone leaves; and short
token lifetimes. MCP clients, though, register themselves on first use
(Dynamic Client Registration), so nobody can type their details in ahead of
time.

## Decision

**Iki is its own OAuth 2.1 authorization server**, built on
django-oauth-toolkit with Iki's client model, validator and pages. Every
RFC 9700 (OAuth security best practice) check the toolkit offers is on:

- Authorization code with **PKCE (S256) required** for every client. No
  implicit or password grants.
- The authorization response carries `iss` (RFC 9207), so a client talking
  to several servers can't be tricked into sending a code to the wrong one.
- Redirect URIs match exactly. They are `https`, or `http` to a loopback
  address (`127.0.0.1`, `[::1]`, `localhost`) for apps on the person's own
  computer (RFC 8252 §7.3). This is enforced when a client registers and
  again at every authorization request.
- Tokens are stored hashed. Access tokens last 60 minutes and refresh tokens
  30 days (`oauth.accessTokenMinutes`, `oauth.refreshTokenDays`). A refresh
  token is replaced each time it's used, and reusing an old one revokes the
  whole chain, because only a thief would have it.
- Metadata at `/.well-known/oauth-authorization-server` (RFC 8414) and
  `/.well-known/oauth-protected-resource` (RFC 9728). API `401`s point at
  the latter in `WWW-Authenticate`, which is how MCP clients find the server.

**Clients register themselves, and an Org Admin approves them.** Dynamic
Client Registration (RFC 7591) is open, so MCP clients work without setup,
but a new client waits for approval by default, as unapproved OAuth apps do
in a GitHub organization with access restrictions. Someone who tries an
unapproved client sees a page that says so, with a button that asks the Org
Admins; the request shows on **Settings → Apps**. Org Admins approve or
block apps there, see who uses each one, and can turn approval off for
smaller teams. Self-registration is limited to the authorization code and
refresh token grants. A client that changes its redirect URIs needs approval
again. Registrations nobody asked about are deleted after seven days.

Client ID Metadata Documents, where a client's ID is an https URL that Iki
fetches, are off by default (`oauth.clientMetadataDocuments`), because Iki's
network policy denies egress to the internet. When on, the same approval
applies, and Iki fetches only public addresses.

**Scopes are `read`, `write`, `run`, `apps` and `admin`.** ADR 0013 named
the first four; `admin` covers managing the organization (people, teams,
service accounts), which no other scope allows. Each permission-service
action maps to one scope. A token can do what **both** its scopes and its
user's roles allow, so a `write` token held by a Viewer still only reads.
A missing scope answers `403` with code `insufficient_scope` and names the
scope in `WWW-Authenticate`.

**People choose what a client gets, scope by scope and project by
project.** The consent page lists the scopes the client asked for, any of
which the person can untick, and asks which projects: chosen ones, or all the
person's projects including ones they join later. The choice is saved, and
the page asks again every time. Changing or revoking it applies to tokens
already issued on their next request. **Account → Apps** lists what each
person has allowed, with a revoke button.

**The CLI and SDK use the device flow** (RFC 8628) as `iki-cli`, a public
client that is Iki's own and needs no approval. The person types the code
from their terminal at `/oauth/device/`, then makes the same choices as on
the consent page. A code belongs to the first person who enters it and to
the client that asked for it; it can be redeemed once; and polling faster
than the interval answers `slow_down`.

**Service accounts use client credentials** that an Org Admin creates on
the service account's page. The secret is shown once and stored hashed.
Each credential has fixed scopes, never `admin`, and acts only as its
service account. Only service-account credentials may use this grant.

**Revoking anything deletes the tokens at once:** deactivating a person or
service account (however it happens, SCIM included), an Org Admin signing
someone out everywhere, blocking a client, revoking consent, or revoking a
credential. Approvals, blocks, consents, revocations and credential
changes are audited. Every change made with a token records the client in
the audit event's `oauth_client`, next to `via=api`.

**The token endpoints are rate limited** per client address (token 120/min,
registration 10/min, device authorization 30/min), and wrong device codes
per person (10/min).

## Consequences

- Positive: MCP clients and the CLI work with no setup, while the security
  team still decides which apps reach the data.
- Positive: a token never does more than its person could, and people can
  narrow it further per project.
- Positive: leaving the company, through SCIM, ends every token at once.
- Negative: with approval on, people wait for an Org Admin the first time
  they use a new app.
- Negative: the toolkit's device-code handling checked too little, and
  oauthlib 4 changed the validator contract under it, so Iki implements the
  device-code checks itself and must re-check them on upgrades.
- Negative: a person's choices apply only to clients that keep asking the
  same server; there is no token exchange or downscoping for chained
  services yet.

## Alternatives considered

- **An external authorization server (Keycloak, the customer's IdP):** one
  more system to run, and customers' IdPs can't express Iki's projects or
  consent. People still sign in through the IdP; Iki issues the tokens.
- **Personal access tokens:** long-lived bearer secrets that end up in
  scripts and notebooks. Refused in ADR 0027, and still refused.
- **No approval, as in an open Slack workspace:** simpler, but regulated
  customers expect an allowlist. It remains a setting.
- **Approval only for clients from outside the organization:** DCR can't
  tell where a client comes from, so every self-registered client is
  treated the same.
