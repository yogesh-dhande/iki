# 0111. SaaS is a fleet of single-organization deployments, run by a control plane

- Status: Accepted
- Date: 2026-11-17
- Refines: [0002](0002-single-tenant-deployments.md)

## Context

ADR 0002 made each deployment serve one organization, run in the
organization's own cloud account. Iki will also be offered as a service, so
that a biotech without a platform team can use it without running its own
infrastructure. That raises the usual question for SaaS: make the
application serve many organizations from one database (a tenant ID on every
row, as most SaaS products do), or run each organization's deployment
separately and share only the operations.

A single organization will also get several registries, the boundaries
within which entities' identities are shared: a CRO keeps each client's
molecules apart. Registries could look like the first step towards tenants.
They aren't. A registry separates groups that share everything else (people,
sign-in, admins, audit, plugins). A tenant shares nothing with another.

Several things rule out one shared database for many organizations:

- **Plugins compile into the schema** (ADRs 0006, 0034). Each organization
  picks its own plugins and versions, and writes its own. Organizations
  sharing a database would have to share one plugin set at one set of
  versions.
- **Regulated customers validate a version** and upgrade on their own
  schedule, after their own testing. A shared application upgrades everyone
  at once.
- **What pharma customers ask for** comes per deployment: their own
  encryption keys, their region, restoring their data alone, a clear
  boundary for an audit, and no one else's workload slowing theirs.
- **A tenant filter on every query** adds a class of breach that projects
  don't have now: one missed filter leaks one company's data to another.

## Decision

**The application stays one organization per deployment.** No tenant ID is
added. Registries are boundaries inside one organization, and a registry is
never shared between organizations: deduplicating molecules across companies
would tell each company what the other holds.

**Iki as a service is a control plane that runs a fleet of deployments.** It
lives outside the application and:

- creates a tenant's deployment from the same OpenTofu modules and Helm chart
  a self-hosting organization uses, so both run the same thing;
- upgrades the fleet in waves, holding each tenant at the version it has
  validated until it says to move;
- monitors each deployment, and runs and tests its backups and restores
  (the DR runbook, per tenant);
- meters use from what deployments already record (resource-seconds, storage)
  and bills it.

**Two kinds of tenant:**

- **Dedicated:** the tenant's own cloud project, cluster and database, as a
  self-hosted deployment has. For large or regulated customers, and those who
  want their own keys or region.
- **Shared cluster:** the tenant's own Kubernetes namespace, database, bucket
  and encryption key, on a cluster shared with other tenants and walled off
  from them by the chart's network policies and service accounts, and a
  namespace that enforces the restricted Pod Security standard, as the dev
  cluster's does. For small organizations, since a
  dedicated stack's floor is several hundred dollars a month at its
  smallest.

Each tenant keeps its own plugins, version and upgrade schedule either way.

The control plane is its own milestone after M1. Until then, nothing in Iki
may assume more than one organization, nor make a fleet harder to run:
deployment, configuration, upgrades, backups and metering stay automatable
from outside, as they are.

## Consequences

- Positive: isolation between companies still comes from separate databases,
  keys and service accounts, never from application code.
- Positive: self-hosted and SaaS deployments are the same software and the
  same infrastructure code, so each is tested by the other.
- Positive: each tenant validates and upgrades on its own schedule, as GxP
  customers need.
- Negative: each tenant has its own database and workloads, so a tenant costs
  more than a row in a shared database. The shared-cluster tier keeps the
  floor down.
- Negative: operating many deployments is real work: the control plane,
  fleet upgrades, monitoring and per-tenant DR must be built and kept up.
