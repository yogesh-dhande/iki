# 0134. A deploy's migrations and their guards are one transaction

- Status: Accepted
- Date: 2026-11-18
- Refines: [0025](0025-append-only-audit-log-and-database-roles.md),
  [0033](0033-plugin-codegen-and-migrations.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md)
- Refined by: [0181](0181-check-a-proposed-plugin-against-existing-records.md),
  on the read-only preflight before migrations and the baseline kept at sync.

## Context

The database holds Iki's guards on plugins' tables with triggers that
`sync_plugins` makes from the build's manifests: unique fields (ADR 0082),
plugins' `unique` and `no_overlap` rules (ADR 0127), locked records (ADR 0125) and references' edges (ADR 0123). The app role's privileges, which
freeze a detached plugin's tables (ADR 0091), come from
`grant_app_privileges`.

The migrate Job ran three steps, each committing on its own: `migrate_deployment`
(each migration a transaction of its own, as Django runs them), then
`sync_plugins`, then `grant_app_privileges`. Old pods keep serving through
a deploy, so they wrote to the database between them, to tables whose
guards didn't match yet:

- **A renamed column.** A rule's trigger reads its fields by name from the
  row. After a field it compares was renamed, it found no value, took the
  record out of the rule and let a duplicate in. A unique field's trigger
  failed every write to the table instead.
- **A rebuilt table.** A migration that drops and recreates a table (a
  plugin's author's own SQL, to rewrite a large table) drops its triggers
  with it. Until the sync, nothing refused a duplicate or a change to a
  locked record. The app role had lost its grants on the table too, so old
  pods failed instead, until the grant step.
- **A failure halfway.** A migration that failed left those before it
  committed, and the sync never ran. The guards stayed out of step, and the
  analysts' views were gone, until a deploy succeeded. So did a sync that
  found records breaking a new rule: the migrations had already committed.
- **A detach.** Between the sync and the grant, a detached plugin's tables
  were still writable.

In a GxP setting a write that skips a uniqueness or lock check is a
data-integrity failure, however brief, and one that later deploys don't
undo: the duplicate stays.

## Decision

**The plugins' migrations, the sync and the grants are one transaction.**
`migrate_deployment` does all of it. Postgres's DDL is transactional, so
other connections see the old schema with its guards until the commit, and
then the new schema with its guards, views and grants, at once. No moment
exists in which a table has changed and its guards haven't. An old pod's
write to a table the migrations changed waits for the commit, on the lock
the change took.

1. The checks that refuse a deploy run first, and change nothing.
2. **Iki's own migrations** run as Django runs them, each in a transaction
   of its own, since some build indexes `CONCURRENTLY`, which no
   transaction can hold. They never touch plugins' tables, and none depends
   on a plugin's. A change to what a guard's trigger function reads ships
   with that function's change, in the same migration, as Iki's have.
3. Then, in one transaction: purges (ADR 0092), rollbacks (ADR 0034), the
   plugins' migrations, the copies kept of them, `sync_plugins`, then
   `grant_app_privileges`. Deferred constraints are checked after each
   migration, as each one's own commit checked them before, so a later
   migration can alter a table an earlier one wrote to. Before the
   rollbacks and before the migrations, the triggers naming the columns of
   the tables they change are dropped: unique fields' and rules' name them
   in `UPDATE OF`, so Postgres won't change such a column's type or drop
   it, and after a rename a trigger would read the column by its old name.
   The sync makes them again, and dropping one locks its table against
   writes until the commit.
4. A plugin's migration must be atomic. Iki's `makemigrations` writes none
   that aren't, and the deploy refuses a hand-written one with
   `atomic = False`, saying why, before anything changes.

If any step fails, nothing of the transaction is kept: not the plugins'
migrations, not the triggers, the views, the plugins' states, the audit
of them, nor the grants. The error says so. Iki's own migrations, if any
ran, stay: they're forward-only, and old pods run on them, as before.

The Job is one container after waiting for Postgres. `sync_plugins` and
`grant_app_privileges` remain commands of their own, for a database
outside a deploy. `migrate_deployment` names the app role as
`grant_app_privileges` does (`--role`, `IKI_DB_APP_ROLE` or
`IKI_APP_DATABASE_URL`), or takes `--no-grant` for a database with one
role, such as a plugin's test database.

Plugins can also be migrated to zero with Django after `sync_plugins`
has installed their guards. When reversing a generated field addition,
Iki drops the unique-field and rule triggers that name that column in
`UPDATE OF` before Django drops the column. Both steps run in the
migration's transaction. Other columns' guards continue to protect the
remaining tables during a partial rollback.

### Alternatives

- **Putting the guards back in each migration's transaction.** A
  migration would end by remaking the triggers of what it changed. But the
  guards come from the build's manifests, which describe the schema after
  the last migration, not after each: a unique column a later migration
  adds doesn't exist yet when an earlier one ends. Getting them right per
  migration means deriving guards from each intermediate state, which Iki
  doesn't model. A failure halfway would still leave the database between
  builds, and the views and grants would still lag. It's more code for
  less.
- **Holding writes until the sync commits.** A maintenance flag that old
  pods check before writing. It stops every write in every table for the
  whole deploy, not only those to tables that changed; only writers that
  check it are held, so a client writing in SQL isn't; and the old pods
  would need to know the flag before the deploy that first needs it. It
  also leaves the failure halfway open: the flag must be lifted by hand, or
  the database stays between builds and frozen.

## Consequences

- No write skips a guard during a deploy, and a failed deploy leaves the
  plugins' schema, guards, views and grants as they were. Tests reproduce
  each case: a renamed field a rule compares, a table rebuilt by a
  migration, and a migration failing after another.
- Analysts' views no longer disappear during a deploy that changes only
  plugins: they're dropped and made again inside the transaction.
- Locks are held longer. A table a migration alters is locked, for reads
  too, until the commit rather than until its migration ends, so a slow data
  migration or recheck (ADR 0127) holds requests to that table for longer.
  Tables the deploy doesn't alter only wait for writes while the sync
  remakes their triggers, as before. A plugin rewriting a very large table
  should do it in a release of its own.
- One transaction holds a lock on every table it changes. A deploy
  changing thousands of tables may need Postgres's
  `max_locks_per_transaction` raised.
