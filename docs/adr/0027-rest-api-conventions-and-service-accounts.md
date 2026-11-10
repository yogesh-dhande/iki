# 0027. REST API conventions, and service accounts

- Status: Accepted
- Date: 2026-11-09
- Refines: [0003](0003-roles-and-permissions.md),
  [0004](0004-backend-stack-django.md),
  [0024](0024-project-access-and-org-admin-boundaries.md)

## Context

Everything Iki does will be reachable over `/api/v1`: the web UI, the
Python SDK, the CLI and MCP clients all use it. Conventions set now are
hard to change once SDKs and customers' scripts depend on them, so they
are settled before most endpoints exist.

Pharma and life-science customers automate a lot: LIMS and ELN syncs,
instrument pipelines, scheduled analyses. Their quality and security teams
expect that automation to act under its own identity, with least privilege,
an owner and an audit trail, and never under a person's account, which
would break attribution and stop working when that person leaves.

## Decision

**The API is django-ninja**, versioned in the path (`/api/v1`), and
described by an OpenAPI document at `/api/v1/openapi.json`. The bundled
Swagger UI is off: it needs scripts and styles Iki's CSP doesn't allow, and
any OpenAPI viewer can read the document.

**Errors are RFC 9457 problem details** (`application/problem+json`), for
every error under `/api/`, including unknown paths, unsupported methods and
crashes:

- `type` is `about:blank` and `title` is the HTTP status phrase. A stable
  `code` (`validation_failed`, `refused`, `csrf_failed`, `rate_limited`,
  ...) is for programs; `detail` is for people.
- Invalid input lists `errors`: body fields as JSON Pointers
  (`{"pointer": "#/name"}`), query and path parameters by name.
- Changes that would break an invariant (the last Admin, a duplicate name)
  are `409` with code `refused` and the same explanation the web pages give.
- `500`s carry the `request_id`, never internals.

**Authorization is the web's.** Every endpoint asks the permission service
(ADR 0003). Things the caller can't see answer `404`; things they can see
but not change answer `403`. A matrix test covers every operation for every
kind of principal and fails when an operation has no row.

**Lists use cursors**, as Slack's and Stripe's APIs do. A page is
`{"items": [...], "next_cursor": ...}`; `limit` is 1–100 (default 50).
Items come oldest first. IDs are UUIDv7, so ordering by ID is ordering by
creation and pages stay stable while rows are added. Cursors are signed,
so clients can't forge them to skip filters.

**Rate limits are per caller**: 600 requests a minute per account by
default (`IKI_API_RATE_LIMIT_PER_MINUTE`), and 60 per client address
without signing in. They are counted in Postgres, so every web pod shares
them. Responses carry `RateLimit` and `RateLimit-Policy`
(draft-ietf-httpapi-ratelimit-headers); `429`s add `Retry-After`. The
other rate limits (sign-in, SAML, SCIM, setup) moved to the same
Postgres counters, which were previously per process.

**Authentication, for now, is the browser session.** Unsafe methods need
the CSRF token in `X-CSRFToken`; pages carry it in a `csrf-token` meta tag
because the cookie is HttpOnly. OAuth 2.1 access tokens, for the SDK, the
CLI, MCP clients and service accounts, come with the OAuth server next;
`401`s already say `WWW-Authenticate: Bearer`.

**Changes are audited with `via=api`.**

**Service accounts are users of kind `service`.**

- Org Admins create, rename, deactivate and reactivate them. Each records
  who created it and a description, for access reviews.
- Each gets an address under `service-accounts.iki.invalid`, kept when it
  is renamed. The `.invalid` domain is reserved (RFC 2606), so the address
  can never be a mailbox or match anyone an identity provider vouches for,
  and project Admins add the account to projects by it, like a person.
  Google Cloud names its service accounts the same way.
- They hold project roles like people, granted per project. They can be
  Auditors or Compute Admins, but never Org Admins: an automation that
  could change who has access would be the easiest way to take over Iki.
- They don't join teams. Teams are people, often decided by identity
  provider groups (ADR 0026); each project a service account needs is a
  separate, visible grant.
- They never sign in through the identity provider, aren't provisioned or
  adopted over SCIM, and don't appear on the People page. A database
  constraint keeps them from looking like a person.
- Deactivating one removes all its access at once. Its OAuth credentials
  (next) will be revoked with it.

## Consequences

- Positive:
  - One error shape, one pagination style and one rate-limit report across
    every endpoint, so the generated SDK handles them once.
  - Automation is attributable to a named, owned account with only the
    project roles it was given, and an Org Admin can stop it at once.
  - Access reviews see service accounts and their grants on one page.
- Negative / trade-offs:
  - Until the OAuth server lands, only the browser can call the API, and
    service accounts have no way to authenticate.
  - Cursor pages can't jump to page N, and lists are ordered by creation
    only. Search and sorting will be filters on top.
  - Counting every request in Postgres costs one small write per request.
    The counter table is UNLOGGED and pruned hourly; a crash loses at most
    the current minute's counts.

## Alternatives considered

- **Personal access tokens now:** quick for scripts, but long-lived bearer
  secrets tied to a person are what pharma security reviews push back on.
  OAuth's device flow for people and client credentials for service
  accounts give short-lived tokens instead.
- **Service accounts as team members:** fewer grants to make, but a team's
  access would silently extend to automation, and identity provider groups
  would need to know about it.
- **Offset pagination (`?page=`):** simple, but pages shift as rows are
  added and deep pages get slow.
- **Rate limits in Django's cache:** per-process with the default cache, so
  limits multiply with replicas; Redis would add infrastructure Iki doesn't
  otherwise need (ADR 0005).
- **Django REST framework:** mature, but ninja's typed schemas generate a
  more precise OpenAPI document for the SDK (ADR 0004).
