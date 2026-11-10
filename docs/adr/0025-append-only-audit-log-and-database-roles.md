# 0025. An append-only audit log, and an app role that can't change the schema

- Status: Accepted
- Date: 2026-11-09
- Refines: [0003](0003-roles-and-permissions.md),
  [0005](0005-postgres-only-data-infrastructure.md),
  [0024](0024-project-access-and-org-admin-boundaries.md)

## Context

Enterprises need to answer "who did what, when, and from where", and to
trust that the answer hasn't been edited. ADR 0024 promised that an Org
Admin reading a project's data is recorded. And a bug or a compromised web
pod must not be able to rewrite history or change the database schema.

## Decision

**Every change, sign-in and setup step writes an audit event** in the same
transaction as the change, so the event exists exactly when the change does.

- An event records:
  - the action (a fixed list, such as `project.role_granted`);
  - the actor and their email at the time;
  - the channel: web, API, MCP, command line or system;
  - the target and its name at the time;
  - the project and team, where there is one;
  - details, which never include secrets;
  - the request ID and the client address.
- Sign-ins are recorded, and so are sign-ins that the identity provider
  authenticated but Iki refused, such as an email conflict.
- Failed or forged OAuth callbacks are only logged, not audited. Anyone can
  send one, and an unauthenticated caller must not be able to grow the
  audit log.

**Nobody can change or delete audit events.**

- **Grants:** the app's database role may only INSERT and SELECT audit rows.
- **Trigger:** refuses UPDATE, DELETE and TRUNCATE from every role, including
  the table's owner. Removing it takes a migration, which is reviewed and
  recorded like any other change.
- **Retention:** events are kept for as long as the installation exists.

**Every event is also written to the `iki.audit` log stream** once its
transaction commits. A SIEM fed from the logs keeps its own copy outside the
database, which is what makes tampering by a database administrator
detectable.

**Auditors read the log** on an audit page with filters (action, person,
project, channel, dates) and stable keyset pagination. Other roles, Org
Admins included, can't see it unless they also hold the Auditor role.

**Two database roles.**

- **Migrator:** owns every table. Only the Helm migrate Job connects as it.
  The Job runs `wait_for_database`, then `migrate`, then
  `grant_app_privileges`. The last step idempotently grants the app role
  exactly what it needs, every deploy.
- **App role:** used by web pods and maintenance jobs. It reads and writes
  rows, but:
  - can't create, alter or drop anything;
  - can't truncate;
  - can only read Django's migration history;
  - can only insert into append-only tables.
- **Readiness:** a web pod whose role has more power than that never turns
  ready, and the log names the problem. That covers a superuser, a role that
  can create roles or databases, a role with CREATE on the schema, the table
  owner or a member of it, and a role able to change the audit log.
- **Helm:** both Secrets are required values. `grant_app_privileges` refuses
  to run if the two roles are the same.
- **Dev cluster:** CloudNativePG manages `iki_app` next to the owner `iki`,
  with a random password per cluster.

## Consequences

- Positive:
  - "Who did what" has one answer, written atomically with the change and
    copied off the database as it happens.
  - A web-layer bug or a compromised pod can't rewrite the audit log, drop
    tables or change the schema.
  - A misconfigured deployment that hands the app the owner's credentials
    fails at rollout, not silently.
- Negative / trade-offs:
  - Operators manage two database roles and two Secrets.
  - The audit table only grows. Archiving old events to cold storage, if an
    enterprise needs it, will be a deliberate, recorded operation by the
    migrator.
  - A database superuser can still disable the trigger. The SIEM copy is
    the safeguard against that, not the database itself.

## Alternatives considered

- **Hash-chained events:** chaining each event to the previous one makes
  tampering visible inside the database. It serializes every write, though,
  and the SIEM copy gives the same assurance without that cost.
- **One database role with application-level checks:** any bug or code
  execution in the web pod would then have DDL rights and could rewrite the
  audit log.
- **Auditing failed callbacks too:** turns the audit log into something an
  anonymous caller can flood. The request logs keep them.
