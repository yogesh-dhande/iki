# 0091. A detached plugin's data is kept and frozen

- Status: Accepted
- Date: 2026-11-15
- Refines: [0008](0008-archive-first-deletion-with-purge-and-tombstones.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md)

## Context

A lab stops using a plugin: a sequencing vendor is replaced, an assay is
retired, a prediction tool is superseded. Its data still matters. Results
were derived from it, reports cite it, and in a regulated lab it has to be
kept for years and shown to an inspector unchanged. But nothing should
write to it any more: no person, no run, no trigger.

ADR 0008 makes this the first of two steps toward removing a plugin, the
second being purge. This ADR is how detaching works.

## Decision

**A plugin is detached in the lockfile.** Its entry's `state` changes from
`active` to `detached`. Like any lockfile change, it is reviewed before
it's deployed, and the deploy that applies it is audited. The plugin stays
installed: its models load, its tables stay and its migrations still
apply, so its data reads as before.

**A detached plugin's tables are read-only to the app.** At each deploy,
`grant_app_privileges` revokes INSERT, UPDATE and DELETE on every table
of a detached plugin: its types, their history, their lists and its
extensions. A web pod whose role can still write one never turns ready
(ADR 0025). So nothing in Iki, a bulk import's SQL included, can change
the data, even through a bug.

**Iki refuses first, and says why.** A refused grant would be a server
error. So creating, editing, archiving or restoring an entity of a
detached plugin's type, or sharing it into another project, is refused
with the plugin named: "dna.DNASequence is from the dna plugin, which is
detached: its entities can be read but not changed." The same rule covers
the rows that live in Iki's own tables, such as revisions and project
links, where grants can't tell plugins apart. An extension from a
detached plugin keeps its fields' values. They can't be set, while the
rest of the entity still changes. Taking an entity out of a project is
still allowed, since it only narrows who can read it.

**What depends on a detached plugin is detached too.** `iki deploy build`
refuses a lockfile that keeps a plugin active while one it depends on is
detached. Its types could extend or refer to tables that no longer take
writes. ADR 0008 allowed a confirmed cascade instead; the confirmation is
listing each dependent as `detached`, so the review shows everything that
stops changing.

**Nothing runs for it.** A detached plugin's actions can't be launched,
its triggers don't fire and can't be turned on, and runs of its actions
still queued are cancelled, each with the reason. Workers aren't sent its
code.

**People can tell.** Its types and entities are labelled "from the
detached plugin X", and the controls that would change them are hidden.

**Each deploy records what it changed about plugins.** `sync_plugins`
keeps each plugin's state, version and commit (`iki_plugin_state`, which
only the migrator writes). It records an audit event for each plugin a
deploy installs, updates, detaches or reattaches, with the values before
and after.

**Detaching can be undone.** Setting the plugin back to `active`
restores its write grants at the next deploy, and the audit log records
it as reattached.

## Consequences

- Positive: a retired plugin's data stays in place, readable and in its
  lineage, and provably unchanged since the deploy that detached it.
- Positive: the database enforces it, not just Iki's code.
- Positive: retiring a plugin is a reviewed, audited change to one file.
- Negative: a detached plugin's code stays in every build until it's
  purged, because its models are what read its tables.
- Negative: a lab can't correct a mistake in a detached plugin's data
  without reattaching it first.
