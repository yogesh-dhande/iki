# 0004. Backend stack: Django 6 with ninja, allauth, SCIM and OAuth toolkit

- Status: Accepted
- Date: 2026-11-09

## Context

Plugins compile YAML into Django apps with real typed tables, and their
migrations are produced by Django's migration autodetector (ADR 0006,
ADR 0007). The backend needs a mature ORM with multi-table inheritance,
reviewable schema migrations, enterprise single sign-on, user provisioning,
an OAuth 2.1 server for MCP and SDK clients, and async endpoints for SSE.
Python also matches the scientific ecosystem that plugin authors and SDK
users already work in.

Iki's first long-term support (LTS) line needs a Django release that will
be supported for years after Iki's own release.

## Decision

- **Django 6.1** now. We move to **Django 6.2 LTS** when it ships (expected
  April 2027). Django 6 requires **Python 3.12 or newer**.
- The app runs under ASGI. Settings come from the environment via
  django-environ.
- **django-ninja** provides the REST APIs with OpenAPI schemas: `/api/v1`
  for users and `/worker/v1` for workers.
- **django-allauth** provides OIDC and SAML single sign-on.
- **django-scim2** provides SCIM 2.0 provisioning (the standard protocol an
  identity provider uses to create, update and deactivate users and
  groups).
- **django-oauth-toolkit** provides the OAuth 2.1 server for MCP clients,
  the SDK, the CLI and service accounts (ADR 0013).
- The **Django admin** is for operators only. It is never the end-user
  interface.

## Consequences

- Positive: Django's migrations, including its interactive rename prompts,
  give plugin authors reviewable schema changes for free.
- Positive: the identity and API pieces are established, maintained
  packages rather than custom code.
- Positive: 6.2 LTS gives Iki's first LTS line a long Django support
  window.
- Negative: until April 2027 we run a non-LTS Django release and must
  upgrade to 6.2 promptly when it ships.
- Negative: we depend on each third-party package supporting Django 6.x
  quickly.
- Negative: Python 3.12 is the minimum supported version.

## Alternatives considered

- **Django 5.2 LTS.** Its support ends in April 2028. That is too soon for
  Iki's first LTS line, which would need a major Django upgrade shortly
  after release.
