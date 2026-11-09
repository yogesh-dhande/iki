# Architecture Decision Records

An Architecture Decision Record (ADR) is a short document that captures one
significant design decision: the context that forced it, what we decided,
and what follows from it. ADRs explain _why_ Iki is built the way it is.
See [ADR 0001](0001-record-architecture-decisions.md) for why we keep them.

## Adding an ADR

1. Copy [`0000-template.md`](0000-template.md) to
   `NNNN-kebab-case-title.md`, where `NNNN` is the next unused number,
   zero-padded to four digits. Numbers are never reused.
2. Fill in the title (`# NNNN. Title`), status, date, and the Context,
   Decision and Consequences sections. Add Alternatives considered when you
   rejected a real option. Keep it short and plain.
3. Add a row to the index below.
4. Submit it for review with the change it governs, or on its own.

## Status values

- **Proposed**: under discussion, not yet in force.
- **Accepted**: in force.
- **Superseded by NNNN**: replaced by a later ADR. Keep the old file and
  link to the new one.

Accepted ADRs are not rewritten. To change a decision, write a new ADR that
supersedes the old one, and update the old one's status.

## Index

| Number                                                           | Title                                                                                     | Status   |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | -------- |
| [0001](0001-record-architecture-decisions.md)                    | Record architecture decisions                                                             | Accepted |
| [0002](0002-single-tenant-deployments.md)                        | Single-tenant deployments: one organization, teams, projects                              | Accepted |
| [0003](0003-roles-and-permissions.md)                            | Fixed org and project roles with one permission service                                   | Accepted |
| [0004](0004-backend-stack-django.md)                             | Backend stack: Django 6 with ninja, allauth, SCIM and OAuth toolkit                       | Accepted |
| [0005](0005-postgres-only-data-infrastructure.md)                | Postgres as the only stateful service, plus S3 object storage                             | Accepted |
| [0006](0006-deploy-time-plugins-as-django-apps.md)               | Deploy-time plugins compiled to Django apps                                               | Accepted |
| [0007](0007-typed-table-per-type-entity-storage.md)              | Typed table-per-type entity storage with history and provenance                           | Accepted |
| [0008](0008-archive-first-deletion-with-purge-and-tombstones.md) | Archive first; deletion is rare, explicit and leaves tombstones                           | Accepted |
| [0009](0009-compute-tiers-and-templates.md)                      | Four compute tiers, one job per worker, sized compute templates                           | Accepted |
| [0010](0010-single-rust-worker.md)                               | One Rust worker binary for all tiers                                                      | Accepted |
| [0011](0011-workflows-and-triggers-in-core.md)                   | Workflows and triggers in core, on Iki's own DAG engine                                   | Accepted |
| [0012](0012-ui-architecture.md)                                  | UI: React host, deploy-time Web Components, apps as trusted compositions                  | Accepted |
| [0013](0013-agents-via-mcp-oauth-approvals.md)                   | Agents through MCP with OAuth 2.1 and human approvals                                     | Accepted |
| [0014](0014-kubernetes-only-deployment.md)                       | Kubernetes-only deployment with Helm and reference OpenTofu modules                       | Accepted |
| [0015](0015-gitops-config-and-export-import-bundles.md)          | GitOps configuration per deployment; data moves only in bundles                           | Accepted |
| [0016](0016-versioned-specs-compatibility-and-lts.md)            | Versioned specs, codemods, deprecation policy and LTS lines                               | Accepted |
| [0017](0017-supply-chain-security.md)                            | Signed, attested artifacts verified at deploy time                                        | Accepted |
| [0018](0018-collaboration-and-assistant-outside-core.md)         | Collaboration and the in-app assistant stay out of core                                   | Accepted |
| [0019](0019-monorepo-and-toolchains.md)                          | Monorepo with uv, pnpm and cargo workspaces and hardened CI                               | Accepted |
| [0020](0020-python-package-naming.md)                            | Python package names: `iki` belongs to the SDK                                            | Accepted |
| [0021](0021-license-and-contribution-model.md)                   | Apache-2.0 license, maintainer-only contributions for now, one reviewed change per commit | Accepted |
| [0022](0022-seaweedfs-for-development-object-storage.md)         | SeaweedFS replaces MinIO as the development S3 stand-in                                   | Accepted |
| [0023](0023-sso-only-sign-in-and-setup-link-wizard.md)           | Single sign-on only, set up through a one-time link                                       | Accepted |
