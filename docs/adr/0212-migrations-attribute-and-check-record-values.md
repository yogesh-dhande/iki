# 0212. Migrations attribute and check record values

- Status: Accepted
- Date: 2026-11-25
- Refines: [0134](0134-a-deploys-migrations-and-their-guards-are-one-transaction.md), [0158](0158-an-upgrade-test-compares-each-revisions-values.md)

## Context

A lab reading a changed value needs the revision that set it. A plugin upgrade also needs an independent expectation for the value its backfill produces. The deploy and the upgrade probe have different evidence available to them.

## Decision

A plugin uses `MigrateValues` to set stored columns on existing records. The operation appends a system revision of kind `migrated` for each changed record, with the changed fields, the plugin, its version, the migration name and the author's reason. It appends a history snapshot of the affected table and an audit event in the same transaction. A reverse callback records its changes the same way. Earlier revisions retain their values. A generated migration adding a field with a manifest default gives its new history column the same initial value.

Before a deploy commits, Iki compares each migrated record table with its latest history snapshots. Each part of a record can have its latest snapshot at a different revision. The comparison includes registry entries, extensions and ordered lists. A mismatch refuses the transaction in either direction and identifies the record and field. A migration must keep the append-only guards active. For handwritten SQL and Python, which can touch any table, Iki compares the guards and retained history before and after each operation. It refuses handwritten snapshots for existing records and detects a guard disabled and enabled again on an empty table. Recorded unit conversions change the column representation with those guards installed. Renames and conversions continue to be recorded under ADR 0148.

Iki refuses a deployment that reuses a subtype field’s name on a parent, or a parent field’s name on a subtype, when the earlier declaration’s history belongs to the other table. Plugin authors can use a distinct name for the new field and keep the original snapshots readable.

The upgrade probe checks earlier revisions and compares each sample's current values after applying the recorded renames and conversions. An added field can take its manifest default. A backfill needs a revision and an exact expected value in the candidate's `after` block on an `upgrade: true` fixture. The keys in `after` name inputs. Each list follows that input's sample order. Each object names only the fields the author wants checked. An expectation applies after the production deploy checks have passed.

## Consequences

A sample's expected value is reviewed alongside the migration that computes it. A rollback that would drop populated history still needs the deployment's usual recovery procedure. Checking a migrated table reads its records and latest snapshots. Handwritten SQL or Python can touch any record. While it runs, the deploy holds a shared lock on the revision table until commit, so concurrent revision writes wait. Comparing snapshots also takes temporary database space proportional to the retained history.
