# 0292. A registry role is one grant, scoped to the registry or to a list

- Status: Accepted
- Date: 2026-11-27
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0270](0270-a-lab-curates-its-own-lists.md)

## Context

ADR 0115 made registrars, whom Org Admins appoint a registry at a time, and
ADR 0270 made list curators, appointed a list at a time in a registry. ADR
0270 describes a curator as a registry role scoped to one list, beside the
registrar's role over the whole registry. They were built as two things:

- two tables, `iki_registrar` and `iki_list_curator`, with the same columns
  apart from the list;
- two pairs of functions to appoint and dismiss, each repeating the checks
  (an active person, never a service account);
- two families of endpoints, `/registries/{id}/registrars/{user}` and
  `/registries/{id}/list-curators/{list}/{user}`, both bare arrays;
- two pairs of audit actions, `registry.registrar_appointed|removed` and
  `registry.list_curator_appointed|removed`;
- a permission lookup that said "a registrar curates every list" by
  returning `None` in place of the set of lists.

The next registry-scoped role (a curator of one type's facts, say) would have
been a third table, a third API and a third audit vocabulary. The REST shapes
and the audit action names are a public contract from M1: an audit action is
a permanent record, and integrations filter on it.

GitHub's organization roles are one assignment of a role to a person, with
the role saying what it covers. Google Cloud IAM binds a role to a member on
a resource. Benchling's registry permissions are one list of grants per
registry. Each is one kind of grant, not one table per role.

## Decision

**A registry role is one grant: a person, a role and its scope.** One table,
`iki_registry_role`, holds every grant: the registry, the person, the role
(`registrar` or `list_curator`), the list (`list_type`), who granted it and
when. The list is null exactly when the role covers the whole registry, as a
registrar's does, and names the list's type for a list curator; the database
checks that pairing. A person holds a role once in each scope: once in the
registry for a registry-wide role, once per list for a list's. A new role is
a new value of `role`, with its scope, and no new table.

Who may grant and hold one is unchanged. Org Admins grant and remove them;
being an Org Admin grants none. The person must be active and a person, never
a service account, since a registry role's changes are a person's, with their
reason. A list curator's list must be an installed lab list. Anyone who sees
the registry sees who holds its roles.

**One family of endpoints, `/registries/{registry_id}/roles`.**

- `GET …/roles` is a cursor page (ADR 0027), `{items, next_cursor}` with
  `limit` and `cursor`, oldest grant first, filtered by `role`, `list` and
  `user`.
- `POST …/roles` with `{role, list, user}` grants it: 201 with the new grant,
  or 200 with the one the person holds already. Granting again changes
  nothing, as GitHub's collaborator `PUT` doesn't. A grant has its own ID,
  since its natural key (a role, an optional list and a person) doesn't make
  one path segment, and a list's qualified name holds a slash.
- `GET …/roles/{role_id}` is one grant, and `DELETE …/roles/{role_id}` takes
  it away (204). Removing a list's curation doesn't need the list installed,
  so a curator of a removed plugin's list can still be removed.

There are no MCP tools for any of it, as there weren't (ADR 0115): these are
a person's decisions.

**One audit pair, `registry.role_granted` and `registry.role_removed`.** Each
event's target is the registry, and its data names the role, the list (null
for a registry-wide role) and the person, by ID and email. The four earlier
actions are gone; pre-M1, no record of them is kept.

**What someone curates is said outright.** The permission service reads a
person's grants in a registry once per request, and says which lists they
curate as a value that is either every list (a registrar's) or the lists
named, never a `None` for "all". A delegated token curates a list only when
both its person and its agent's service account do (ADR 0073); a service
account holds no registry role, so today that refuses every such token, as
before.

## Consequences

- One place grants and removes a registry role, with its checks, and one
  place reads what someone holds.
- Integrations read every registry role from one page and filter it, and
  follow one pair of audit actions.
- A grant is addressed by its ID for reading and removing it. Granting is
  idempotent on the role, list and person.
- Pre-M1, the org migrations were rewritten: the registrar table is no longer
  made, and `iki_registry_role` replaces `iki_list_curator`. Development
  databases are regenerated.

## Alternatives considered

- **Keep two tables behind one API.** The storage would still need a third
  table for the next role, and each permission check would read two.
- **`PUT /registries/{id}/roles/{role}/{user}`, with the list in the query.**
  It keeps GitHub's idempotent `PUT` by natural key, but a scope in the query
  string isn't part of the resource's path, and a list's qualified name in
  the path needs an encoded slash.
- **`/registries/{id}/curators`, as the review proposed.** A registrar is a
  role of a registry, not only a curator of it; `roles` names what the grants
  are.
- **Org Admins as implicit registrars.** Unchanged from ADR 0115: they manage
  access, and read no project's data by their role.
