# 0094. A plugin's upgrade is tested from an earlier commit

- Status: Accepted
- Date: 2026-11-15
- Refines: [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0093](0093-plugin-action-tests-run-fixtures-through-the-worker.md)

## Context

A deployment upgrades a plugin by pinning a newer commit. Its migrate Job
then applies the plugin's new migrations to years of entities and their
history. A migration that works on an empty database can still fail on
real rows: a field made required that some entities lack, a type change
their values don't fit, a data migration with a bug. And if the upgrade
has to be undone, the rollback reverses those migrations from their kept
copies (ADR 0034), which only works if each can be reversed.

`iki plugin makemigrations --check` shows the migrations match the
manifests. It doesn't run them.

## Decision

**`iki plugin test --upgrade-from REF` upgrades sample data.** It takes
the plugin as it was at a git commit, and as it is now, and builds each
with the plugins given by `--with`. On a scratch database it makes on the
server given by `--database` (or `$IKI_TEST_DATABASE_URL`), it then:

1. migrates to the earlier build, as a deploy would (`migrate_deployment`,
   then `sync_plugins`), and creates the sample entities;
2. migrates to the current build, and checks that every sample is still
   there, at the same revision, with every revision of its history still
   readable;
3. deploys the earlier build again, as a rollback would, and checks the
   samples have the values they started with.

The database is dropped at the end, passed or not.

**Its samples are the earlier commit's own.** They are the input
entities of the action tests in `tests/` at that commit (ADR 0093),
which its authors wrote to be valid then. Nobody keeps a second set of
data in step with the first. A value that names a file is left out of
its sample.

**What fails it.** A migration that fails, a sample that's gone or
changed revision, a revision of its history that can't be read, or a
rollback that loses a value. A rollback the deploy refuses also fails it,
with the deploy's reason. Such an upgrade can't be undone once it's
deployed, and that should be decided in review, not found in production.
`--no-rollback` skips step 3 for an upgrade meant to be one-way.

**The template's CI runs it** against the pull request's base commit,
with a PostgreSQL service.

## Consequences

- Positive: an upgrade is run on data before a deployment runs it, and
  its rollback is proven too.
- Positive: it needs no fixtures of its own.
- Negative: it needs PostgreSQL, so it's slower than the action tests,
  and samples are only as varied as the earlier commit's tests.
