# 0014. Kubernetes-only deployment with Helm and reference OpenTofu modules

- Status: Accepted (development object store amended by [0022](0022-seaweedfs-for-development-object-storage.md))
- Date: 2026-11-09

## Context

Iki runs a web server, an MCP server, a scheduler, backend worker pools,
container-tier jobs and a migration step. It needs network isolation,
admission-time signature checks (ADR 0017), and autoscaling. Each
enterprise operates its own deployment (ADR 0002), so the install must be
repeatable and well tested. Development and production should behave the
same.

## Decision

- **Kubernetes is required.** There is no single-VM or Docker Compose
  install.
- One **Helm chart** deploys web (ASGI), mcp, scheduler, the backend worker
  pool, and the migrate Job as a pre-upgrade hook. It includes
  NetworkPolicies, the `restricted` Pod Security standard, and horizontal
  pod autoscaling.
- **Local development** uses kind (Kubernetes in Docker) and Tilt (a tool
  that rebuilds and redeploys on file changes) with the **same Helm
  chart**. CloudNativePG, MinIO and Keycloak run in the cluster.
- **Reference OpenTofu modules for AWS** create the VPC, EKS with CPU and
  GPU node groups, RDS Postgres Multi-AZ with point-in-time recovery,
  versioned and encrypted S3, KMS, IRSA roles, VM-worker instance profiles
  and launch templates, optional FSx, cross-region backup copies, and
  External Secrets wiring. GCP comes later.
- Production is **Multi-AZ with cross-region backups**. Targets are RPO of
  at most 5 minutes and RTO of at most 4 hours (how much data may be lost,
  and how long recovery may take). A documented restore runbook is backed
  by an automated restore test.

## Consequences

- Positive: one deployment target to secure and test.
- Positive: local, CI and production use the same chart, so environment
  differences surface early.
- Positive: the container tier, NetworkPolicies and admission policies all
  rely on Kubernetes anyway.
- Negative: enterprises without Kubernetes skills face a steep entry
  cost.
- Negative: local development needs a machine that can run a kind
  cluster.
- Negative: until GCP modules exist, non-AWS enterprises must write their
  own infrastructure code.
