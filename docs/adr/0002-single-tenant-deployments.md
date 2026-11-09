# 0002. Single-tenant deployments: one organization, teams, projects

- Status: Accepted
- Date: 2026-11-09

## Context

Enterprises deploy Iki in their own cloud account (AWS first, GCP later).
Their data is unpublished research and intellectual property. Each
enterprise also writes its own plugins, and plugins compile into real
database tables (ADR 0006, ADR 0007). Two enterprises with different
plugins cannot share one schema.

## Decision

Each Iki deployment serves exactly one organization.

- Inside a deployment the hierarchy is: one org, then Teams, then Projects.
- Access is granted through `Membership(project, user | team |
service_account, role)` (ADR 0003).
- One entity can appear in several projects through `ProjectLink(entity,
project)`. Each entity still has one `owner_project`.
- There is no tenant ID column and no cross-organization sharing inside a
  deployment.
- Data moves between deployments (for example from staging to production,
  or between enterprises) only through explicit export/import bundles
  (ADR 0015).

## Consequences

- Positive: isolation between enterprises comes from separate
  infrastructure, not from application code. A bug in a query filter cannot
  leak one enterprise's data to another.
- Positive: each enterprise controls its own plugins, schema, region,
  encryption keys and upgrade timing (ADR 0016).
- Positive: the data model and authorization logic are simpler without a
  tenant layer.
- Negative: every enterprise runs its own infrastructure, with its own cost
  floor and operations work. Iki must therefore ship strong operations
  tooling: a Helm chart, reference OpenTofu modules, and tested disaster
  recovery (ADR 0014).
- Negative: there is no shared hosted instance. Fixes reach users only when
  each enterprise upgrades its deployment.
