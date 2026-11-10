# 0023. Single sign-on only, set up through a one-time link

- Status: Accepted
- Date: 2026-11-09
- Refines: [0003](0003-roles-and-permissions.md), [0004](0004-backend-stack-django.md)

## Context

Iki is deployed per organization (ADR 0002). People sign in with the
organization's identity provider: OIDC now, SAML and SCIM provisioning
later. The plan called for a local bootstrap administrator that exists only
until single sign-on is configured in a first-run wizard.

A local administrator account needs a password. That password has to be
delivered somehow: a default (unsafe), a value in the logs (readable by
anyone with log access, and often shipped to a SIEM), or a Helm value (stored
in Git and in the release history). It is also an extra way in that outlives
its purpose unless someone remembers to remove it.

## Decision

**Iki has no local passwords at any point.** All sign-in goes through the
configured identity provider. Django's password login is not installed:
the only authentication backend is allauth's, every user has an unusable
password, `createsuperuser` refuses, and allauth's password, email and
account-linking pages are not mounted.

**Setup starts from a one-time link.** An operator runs
`iki-server setup_link` inside the cluster (`kubectl exec`). Being able to
do that is the proof of authority, so nothing secret is ever written to
logs, values or Git.

- The link works once, for 60 minutes. Creating a link revokes earlier ones.
- Only the SHA-256 of the token is stored.
- The token sits in the URL fragment, which browsers never send to servers,
  so it can't reach access logs, traces or `Referer` headers. A small script
  moves it into a form; without JavaScript, the code can be pasted.
- Opening the link exchanges the token for a setup grant in that browser's
  session (with a new session key). The grant lasts 60 minutes.

**The wizard proves sign-in works before enabling it.**

1. Name the organization.
2. Enter the OIDC provider's issuer, client ID and client secret. Iki fetches
   the discovery document from inside the cluster and checks it:
   - the issuer must match exactly;
   - all endpoints must be https;
   - the authorization code flow must be supported;
   - PKCE S256 must be available;
   - client-secret authentication must be supported.

   Redirects are not followed and responses are size-capped.

3. Sign in through the provider. Until setup finishes, the new provider
   works only for sessions that hold the setup grant.
4. Confirm. The provider is enabled, and the person who signed in becomes
   the first Org Admin (ADR 0024). The grant and its token are revoked.

**Break-glass recovery uses the same path.** `setup_link --recover`
reconfigures sign-in on an installation that is already set up, for example
after an identity provider migration. Whoever completes it becomes an
Org Admin. The UI marks recovery mode clearly, and it is logged.

**Identity rules:**

- **Matching:** accounts are keyed by the provider's subject identifier. Each
  issuer gets its own provider ID, so subjects from different issuers never
  collide.
- **No email linking:** Iki never links accounts by email address. That would
  let anyone who controls a matching address at another provider take the
  account over.
- **Email uniqueness:** emails are unique, compared case-insensitively
  (a nondeterministic ICU collation), and required. A sign-in that would
  collide with another account's email is refused with an explanation.
- **Profile sync:** name and email follow the identity provider on every
  sign-in. New people get an account on first sign-in, with no privileges.
- **Secret storage:** the client secret is encrypted at rest with Fernet keys
  from `IKI_ENCRYPTION_KEYS`. The keys can be rotated with
  `iki-server rotate_encryption`. allauth's own app table, which stores
  secrets in plain text, is never used.

**Sessions** are server-side, in Postgres, and linked to their user:

- **Lifetimes:** an idle timeout (8 h) and an absolute lifetime (24 h) after
  sign-in, both configurable.
- **Before sign-in:** sessions last only 15 minutes.
- **Cookies:** browser-session only, `HttpOnly`, `__Host-` prefixed over
  https. The session cookie is `SameSite=Lax`, needed for the identity
  provider's redirect back; the CSRF cookie is `SameSite=Strict`.
- **Revocation:** deactivating a user deletes their sessions immediately.

**Pages** carry a strict CSP from Django's built-in middleware:

- `default-src 'none'`, with only same-origin scripts, styles, images and
  connections;
- no inline code, and Trusted Types required;
- `frame-ancestors 'none'` and `base-uri 'none'`.

`form-action` adds only the identity provider's authorization origin,
because browsers apply it to the redirect that follows the sign-in form.

Responses also carry COOP, COEP, CORP, Permissions-Policy and HSTS (over
https), and are `no-store` by default. CSP reports go to `/csp-report/`.
They are logged without query strings and rate limited.

Unauthenticated endpoints that cause work (starting a sign-in, exchanging a
setup link, CSP reports) are rate limited per client address in each web
process. The shared, Postgres-backed rate limiter arrives with the API
foundation.

Web pods may reach the identity provider over HTTPS on public addresses
only. The NetworkPolicy blocks private ranges, link-local addresses (cloud
metadata services) and loopback, so the discovery check can't be turned
against the cluster.

## Consequences

- Positive:
  - There is no password to leak, guess, phish or forget to disable, and no
    default credential, ever.
  - The person setting up Iki proves both cluster access and a working
    identity provider before anyone can sign in.
  - Recovery doesn't need database surgery.
  - Accounts can't be taken over through email addresses.
- Negative / trade-offs:
  - Without a working identity provider and cluster access, nobody can sign
    in. That is intended: recovery runs through the cluster.
  - Switching to a different issuer creates new provider IDs, so existing
    people get new accounts unless they are migrated. An admin migration
    tool is future work (SCIM provisioning makes this rarer).
  - Rate limits are per web pod until the shared limiter lands.
  - Signing out of Iki doesn't sign people out of their identity provider.
    The signed-out page says so.

## Alternatives considered

- **Local bootstrap admin with a generated password** (printed to logs, or
  set in Helm values): leaks through logs, Git or release history, and needs
  a removal step people forget.
- **SSO configured only through Helm values / GitOps:** no feedback loop, and
  a typo locks everyone out. Configuration-as-code for the identity provider
  can be added later, on top of the same checks.
- **Allauth's built-in app table:** stores client secrets in plain text.
- **Linking accounts by verified email:** convenient, but a known account
  takeover path when more than one identity source exists.
