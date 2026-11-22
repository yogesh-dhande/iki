# 0181. Check a proposed plugin against existing records before promotion

- Status: Accepted
- Date: 2026-11-22
- Refines: [0015](0015-gitops-config-and-export-import-bundles.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0134](0134-a-deploys-migrations-and-their-guards-are-one-transaction.md)

## Context

A deployment build verifies that each pinned plugin has migrations for the
schema it declares. Whether a change fits the target deployment's records
is a different question. For example, a reviewer deciding whether to
promote a new required field needs to know how many records lack a value.
Until now the first feedback about a conflict could come from the migrate
Job, after the promotion had been approved.

## Decision

An Org Admin with access to all projects can check a proposed plugin's compiled `manifest.json`
against the target deployment through its Plugins page, the API or
`iki plugin preflight`. The input is the declarative manifest from the
build. The check runs in one read-only PostgreSQL snapshot and never runs
the candidate's migrations. This lets an admin review the effect on live
records while people continue working.

At a successful sync, the migrator keeps the installed manifests and the
configuration revision together in the database. The revision is the
lockfile's SHA-256, or a digest of the installed manifests for builds made
without a lockfile. The migrate Job can therefore read records using the
schema that stored them even though the Job is running the proposed build.
The report identifies that baseline and separately gives the digest of the
proposed manifest. A change to the proposed manifest changes its digest.

The report groups findings by type. It gives affected counts and example
HRIDs for backfills, values outside a new field's allowed values or
bounds, rules and uniqueness conflicts, and records in a removed state.
For a change of canonical unit it describes the conversion or reports why
the old unit cannot be converted. For each changed concrete type, the check
uses the index on type, HRID and record ID to select up to 500 records in
that order. One extra key tells it whether there are more. Each type says
how many records were checked and whether the check was sampled. Counts
in a sample are lower bounds. The check also caps list-table reads at
4,096 items per selected list table for the sample and reference lookups
at 1,024 targets per type. It excludes records with partially read lists
and reports rules with unread references as unchecked. For an amount
ledger, changing the declared unit leaves existing balances in their
stored unit. The report identifies the records needing a data migration.

`migrate_deployment` repeats the check against the target before making
changes. A blocking finding or incomplete sample stops the deploy.
An operator who has reviewed the result can acknowledge the specific
candidate digest in the deployment's Helm values. The migrations and
their database guards still run in the transaction of ADR 0134. The sync
records the new baseline in that transaction, so a rollback of the
transaction also rolls back the baseline.

## Consequences

An admin can download the report for a promotion review and use its
example IDs to find records to correct. Production and staging can give
different findings for the same candidate because their data differs.
A read-only snapshot describes the records visible when the check ran.
The deploy repeats the check because records may change after review.
Hand-written data migrations may fix conflicts or introduce changes that
the manifest alone cannot predict. Reviewers still review those migrations
alongside the report.
