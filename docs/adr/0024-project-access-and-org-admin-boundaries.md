# 0024. Project access comes only from membership

- Status: Accepted
- Date: 2026-11-09
- Refines: [0003](0003-roles-and-permissions.md)

## Context

ADR 0003 fixed the roles: Org Admin, Compute Admin and Auditor across the
organization, and Viewer, Editor, Runner and Admin in each project. It
didn't say:

- how people get project roles;
- whether running the installation also means seeing all of its data;
- who may create projects;
- what archiving does.

Iki holds unpublished research, so these answers decide who can read it.

## Decision

**Project roles come only from project memberships.** A membership grants
one role to one person, or to every member of one team (including people who
join the team later). Someone's role in a project is the highest of their
direct grant and their teams' grants.

**Org Admins see every project, but read its data only as members.**

- They see every project's name, team, description and members, and can
  manage any project's members.
- That includes adding themselves as Admin, which is how a project whose
  Admins have left is recovered.
- Reading the data takes that explicit, visible membership. The audit log
  (next release) records it.
- Compute Admins and Auditors get no project access from their org roles.

**Any member of a team can create a project in that team** and becomes its
first Admin. Projects belong to exactly one team, whose compute limits apply
and which pays for its runs. Owning a project doesn't give the team's
members access to it. Only memberships do.

**Projects and teams people can't see answer 404**, so their existence
doesn't leak. Pages they can see but not change answer 403.

**Invariants hold however a change is made:**

- **Org Admins:** the organization keeps at least one active Org Admin.
  Otherwise only cluster access (`setup_link --recover`) could fix it.
- **Project Admins:** a project keeps at least one active Admin, counting a
  team grant with at least one active member.
- **Deactivation:** nobody can deactivate themselves. Deactivated people get
  no new roles. Deactivation signs them out everywhere, but their
  memberships stay, so reactivating restores their access.

**Archiving, not deleting** (ADR 0008):

- An archived project is read-only. Its Admins can restore it, and its
  members can still be managed.
- An archived team gets no new projects. Its projects and their access are
  unchanged.
- Teams with projects can't be deleted.

**Django's permission system is not used.** Users have no `is_superuser`,
`is_staff`, groups or per-user permissions. The only source of truth is
`iki_server.org.permissions`. This also removes Django's superuser bypass,
which would otherwise ignore every rule above. Installations set up before
this change have their first administrator converted to Org Admin by a
migration.

**Every rule is tested as a matrix.** Every principal (anonymous,
deactivated, outsider, team member, each org role, each project role, roles
through a team, mixed grants) is tested against every action. The expected
table is written by hand, and any action it doesn't list must be refused. A
second matrix covers every page and form, and fails if a new URL isn't in it.

## Consequences

- Positive:
  - Sensitive data is need-to-know, even for the people who run Iki.
    Breaking that requires a visible, recorded act.
  - Scientists start projects without waiting on an admin.
  - Orphaned projects are recoverable without database access.
  - One permission system instead of two, so there is no superuser path
    around the rules.
- Negative / trade-offs:
  - An Org Admin who needs to help inside a project must first join it.
    That is the intent, but it adds a step.
  - People must sign in once before they can be added to teams or projects.
    SCIM provisioning (planned) removes this.
  - The Django admin, when it arrives for operations, has to be wired to
    the permission service rather than to Django's permissions.

## Alternatives considered

- **Org Admins read everything:** simpler, but every admin account becomes
  a key to all research data.
- **Org Admins can't join projects:** strictest, but an orphaned project
  would need cluster-level recovery.
- **Only Org Admins create projects:** tighter control, but every new piece
  of work waits on an admin.
- **Team members implicitly see their team's projects:** convenient, but it
  makes sharing a project with one person from a large team impossible.
