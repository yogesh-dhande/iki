# 0092. Purging a detached plugin, under legal holds

- Status: Accepted
- Date: 2026-11-15
- Refines: [0008](0008-archive-first-deletion-with-purge-and-tombstones.md),
  [0091](0091-a-detached-plugins-data-is-kept-and-frozen.md)

## Context

A detached plugin's data is kept and frozen (ADR 0091). Some day a lab
wants it gone: the retention its records needed is over, or it never
needed keeping. ADR 0008 makes purge the way entity data leaves Iki: an
explicit step that only a detached plugin can take, with a dry-run
report, a typed confirmation, legal holds respected, a retention window,
and tombstones.

Legal holds come first. A lab under litigation, an inspection or a
regulator's request must keep what the matter covers, whatever its
retention says. Slack, Google Vault and Microsoft Purview call this a
legal hold. It suspends deletion, it's placed by the people who answer
for compliance, and the people it covers aren't told.

## Decision

**An Org Admin puts a project on legal hold, for a matter.** The hold
names the matter, such as a case, an inspection or a request, and can
carry a note. A project can be under several holds, one per matter. While
any is on, nothing in the project is deleted:

- run artifacts stay past their retention;
- no plugin can be purged while any of its data is owned by the project
  or shared into it.

People keep working in the project as usual. Archiving still works,
since an archived project keeps everything.

**Holds are confidential.** They're listed under Settings → Legal holds,
which Org Admins manage and Auditors can read. Placing and releasing a
hold are audited. The project's members aren't shown it, and its webhooks
aren't sent it. An artifact kept past its retention says only that.

**Purge starts with a report, and a person's typed confirmation.** An
Org Admin asks for one on the Plugins page. First, a dry run lists what
would go:

- each type's entity count;
- the projects they're in, and any of those on hold;
- what still refers to them: other plugins' entities, collections, saved
  views, apps and runs.

A hold refuses the purge outright. Otherwise, the Org Admin gives a
reason and types the plugin's name, as GitHub has you type a repository's
name before deleting it.

**Then it waits.** A confirmed purge is scheduled. By default it runs
after 30 days (`IKI_PURGE_WAIT`, at least 7 days), as AWS schedules a key
for deletion, and an Org Admin can cancel it until then. A hold placed
in the meantime stops it again. When the wait is over, the lockfile's
`state: purged` lets the next deploy carry it out. A purge isn't
something one click does in an afternoon.

**Each entity leaves a tombstone.** Its core row stays, with its ID,
HRID, type, owner project and creation, so references, runs and lineage
still resolve. It gains when it was purged, by whom, why, and a SHA-256
of its last values, which proves what existed without keeping it. Its
values, history and lists go with the plugin's tables. The HRID is never
handed out again. Lineage and references show the entity as purged
instead of failing. Reading it answers 410 Gone with its tombstone, to
those who could read it; to anyone else it's 404, as it was.

**The deploy drops the tables.** Maintenance writes the tombstones once
the wait is over. Then a lockfile with `state: purged` lets the migrate
Job finish, in one transaction. It refuses, changing nothing and saying
why, unless the purge is due and tombstoned, no hold has come, and
nothing still installed depends on the plugin. It then:

- drops the plugin's tables whole, with its fields on other plugins'
  types;
- deletes what its entities hold in tables they inherit from other
  plugins (a DNA sequence's `core_sequence` row), history included;
- deletes their unique values and identity hashes;
- forgets the plugin's migrations, marks the purge done and audits it.

It drops the tables instead of reversing the plugin's migrations from
their stored copies (ADR 0034). A reversal runs the plugin's own reverse
code, which could change rows Iki keeps, such as the types the
tombstones name. History is append-only for every role, so only the
migrate Job lifts that, for those rows, inside its transaction. The
Plugins page keeps the purged plugin on record: who asked, why, what it
deleted and when.

## Consequences

- Positive: a lab can retire a plugin's data for good, through one
  reviewed, audited, delayed, reversible-until-late path.
- Positive: legal holds protect what a matter covers from every deletion
  Iki does, not just purge.
- Positive: tombstones keep lineage, HRIDs and proof of what existed.
- Negative: purging takes at least a week and a deploy, by design.
- Negative: a hold on a busy project keeps every artifact in it, so
  storage grows until it's released.
- Negative: holds are as wide as a project. A matter that covers only
  some of a project's entities still holds all of it.
