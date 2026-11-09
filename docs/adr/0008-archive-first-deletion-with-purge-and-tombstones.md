# 0008. Archive first; deletion is rare, explicit and leaves tombstones

- Status: Accepted
- Date: 2026-11-09

## Context

Iki's value is lineage: any result can be traced back through runs and
revisions to its inputs. Deleting an entity breaks every lineage graph,
provenance record and reference that points at it, and makes past runs
impossible to reproduce. So features should avoid deleting data.

But "nothing is ever deleted" can't be a promise. Some data has to go:

- removing a field from a plugin drops its column in a migration;
- run artifacts expire after their retention period, and worker caches
  evict files;
- enterprises remove plugins they no longer use, together with their data;
- legal or privacy requests may require erasing specific records.

## Decision

"Nothing is deleted" is a **design guideline for building features**, not
a guarantee made to users.

- **Archive is the default.** User-facing features let people **archive**
  entities (set `archived_at`) rather than delete them. Archived entities
  stay in history and lineage.
- **Deletion needs a reason.** A feature that deletes data must say why
  archiving isn't enough. The deletion must be explicit, permission-checked
  and audited, and it should leave a tombstone wherever lineage or
  references could point at the deleted data.
- **Plugin purge** is the main deletion path for entity data. It happens in
  two lockfile steps (ADR 0006):
  1. **`state: detached`.** The plugin's models stay loaded. Its tables
     become read-only (write grants are revoked). Its actions and triggers
     are disabled, and its worker pools are drained. The UI labels the data
     "from uninstalled plugin X". A dependency guard requires dependent
     plugins to be detached first, or a confirmed cascade.
  2. **`state: purged`**, allowed only from detached:
     - a dry-run report of counts, references from other plugins, and
       affected projects;
     - a typed confirmation, with legal holds respected;
     - a retention window, after which `migrate <plugin> zero` drops the
       plugin's tables.
- A **tombstone** records ID, HRID, type, content hash, the run or actor
  that deleted the data, and the reason. Lineage and references resolve to
  "deleted" or "purged" instead of failing.
- Other deletion paths, such as erasing a single record for a privacy
  request, are allowed. Each one gets its own ADR when it is designed.

## Consequences

- Positive: lineage and provenance stay intact in normal use, and old runs
  stay explainable.
- Positive: when data does have to go, there is a standard pattern for it:
  explicit, audited, and tombstoned.
- Positive: tombstones keep HRIDs unique and let content hashes prove what
  existed.
- Negative: archive-first means storage keeps growing unless retention or
  purge removes data.
- Negative: users can't remove a mistaken entity themselves, only archive
  it.
- Negative: because deletion isn't forbidden outright, every deletion path
  must be reviewed for how it affects lineage.
