# 0152. A token given some projects acts only in them, and its app's access ends everywhere at once

- Status: Accepted
- Date: 2026-11-19
- Refines: [0028](0028-oauth-authorization-server.md),
  [0030](0030-tasks-live-updates-and-notifications.md),
  [0031](0031-signed-webhooks.md),
  [0073](0073-unattended-agents-act-for-a-person-by-delegation.md)

## Context

When someone allows an app (ADR 0028), the consent page asks which
projects it may use: only the ones they choose, or all of them. The choice
was applied to project actions only. An action on the organization has no
project to compare, so the choice didn't apply to it: an Org Admin's
token for one project could add an organization webhook, which hears of
every project, and an Auditor's could read the costs of every project. The
person chose one project and the app got the organization.

The person's choice was also read once, when a token's request began. An
event stream lasts up to 30 minutes, and kept the choice, and the roles, it
started with. A webhook an app added kept sending after the person narrowed
or revoked their consent, because nothing linked it to the app.

GitHub's fine-grained tokens and GitHub Apps choose repositories and
organization permissions separately: a token for some repositories gets no
organization permission unless it's asked for and granted. A GitHub App's
webhooks stop when it's uninstalled. Okta and Slack end an app's access
everywhere at once when it's revoked. Pharma security teams read the
consent page as the whole of what an app may do, and expect revoking to
stop all of it.

## Decision

**A token given only some projects acts in those and nowhere else.** It
takes no action on the organization, its teams or its registries, whatever
its scopes and its person's roles: no costs across projects, no
organization webhooks or settings, no compute, people, teams or service
accounts. Nor does it import a bundle into one of its projects, which adds
people to the organization as an Org Admin (ADR 0096). The rule is one check in the permission service, so it covers
every endpoint, MCP tool and event stream at once. An action refused for it
answers `403` with code `token_limited_to_projects`, and says to allow the
app all projects, when the person's roles would allow it. Lists of teams
are empty for such a token. It may still read what every signed-in person
reads about the deployment (types, actions, registries, signing keys) and
the person's own things (their notifications, their settings). The consent
page says so beside the choice. A delegation's choice (ADR 0073) is the
same choice; a delegation for all the person's projects is still held to
where its service account may go, as before.

**Every check of a token reads the person's choice as it stands.** API
requests already did. An open event stream now checks its session or token
again before each event and at each heartbeat, exactly as a request would:
the token, its consent now (narrowed, revoked or expired), and the person
and any delegated service account active. Roles are read again then too, so
someone no longer an Org Admin or a Compute Admin stops hearing of what
they no longer may. A stream whose credentials no longer hold sends
`signed_out` and ends.

**An app's webhooks last as long as its access.** A webhook added with a
token records the grant it was added under (the person's consent to that
app). It sends only while the app could still add it: its person's consent,
scopes, projects and roles as they are now. When they no longer allow it,
because the person narrowed or revoked their consent, the app was blocked,
the delegation ran out, or the person left or lost the role, Iki turns the
webhook off instead of sending, audits `webhook.disabled` with reason
`app`, and tells the webhook's managers. Iki looks when the consent
changes, before each message, and in `iki-server maintain`. A webhook a
person added in the browser is theirs, as before. Whoever turns an app's
webhook back on answers for it from then on: a person in the browser makes
it theirs; another app makes it its own.

## Consequences

- What the consent page shows is all an app may do, and revoking or
  narrowing it applies at once to requests, open streams and webhooks.
- Tools that manage the organization (the CLI used by an Org Admin, an
  agent that triages costs) need the person to allow them all projects.
  The refusal says so.
- A webhook's sending depends on its app's access, so a person who leaves
  takes their apps' webhooks with them. Their managers are told, and can
  turn each back on as theirs.
- Streams check credentials and roles before each event, a few small
  queries each. Events are identifiers only and infrequent per stream, so
  this costs less than the requests the page then makes.

## Alternatives considered

- **Filtering organization reads to the chosen projects** (costs of those
  projects only): it fits reads, but not organization webhooks, settings,
  compute or people, which have no project to filter by. One rule is
  easier to reason about, and a project's own costs are already at
  `/projects/{id}/costs`.
- **A separate organization permission on the consent page**, as GitHub
  has: more to choose, for a case "all my projects" already covers.
- **Deleting an app's webhooks when its access ends:** loses their
  history and deliveries, which an audit may need. Turning them off keeps
  both and lets a manager take them on.
