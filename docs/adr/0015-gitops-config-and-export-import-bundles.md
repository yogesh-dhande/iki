# 0015. GitOps configuration per deployment; data moves only in bundles

- Status: Accepted
- Date: 2026-11-09

## Context

Enterprises run several deployments, typically dev, staging and prod.
Plugin versions, plugin settings and compute templates must be reviewed,
reproducible and promotable from one deployment to the next. Changes made
by clicking in an admin screen are hard to review and easy to lose.
Separately, teams sometimes need to move data, for example promoting
validated reference data from staging to prod.

## Decision

- Each deployment has its own **GitOps config**: a git repo whose contents
  define the deployment. It holds:
  - `plugins.lock.yaml` (ADR 0006);
  - plugin settings, validated against each plugin's settings schema at
    build time;
  - compute templates (ADR 0009);
  - secret references. Secret values stay in AWS Secrets Manager and reach
    the cluster through External Secrets.
- Changes land through git review and a deploy. The admin plugins screen
  shows the lockfile state read-only.
- **Data moves between deployments only through explicit export/import
  bundles**, exposed by the REST API. Deployments never share a database,
  and there is no live replication between them.

## Consequences

- Positive: every configuration change has a reviewer, a commit and a
  rollback path.
- Positive: promotion from dev to staging to prod is a git operation.
- Positive: secrets never appear in the config repo.
- Positive: data movement is deliberate and auditable.
- Negative: configuration changes need a deploy, which is slower than an
  admin toggle.
- Negative: the bundle format is a new compatibility surface. It must be
  versioned like the other specs (ADR 0016) and carry enough provenance to
  keep lineage intact.
