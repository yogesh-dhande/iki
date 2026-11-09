# 0005. Postgres as the only stateful service, plus S3 object storage

- Status: Accepted (development object store amended by [0022](0022-seaweedfs-for-development-object-storage.md))
- Date: 2026-11-09

## Context

Every enterprise runs its own deployment (ADR 0002). Each extra stateful
service it must run adds backups, high availability, upgrades, credentials
and attack surface. Iki also needs some state changes to happen together:
committing a run's outputs and marking its job done should succeed or fail
as one unit.

## Decision

Postgres holds all structured state:

- **Entity tables**, history and provenance (ADR 0007).
- **The job queue.** Workers claim jobs with `SELECT ... FOR UPDATE SKIP
LOCKED`. `SKIP LOCKED` lets many consumers each claim a different row at
  the same time, skipping rows another consumer has locked instead of
  waiting for them. `LISTEN/NOTIFY` (Postgres's built-in publish/subscribe
  messages) wakes waiting consumers when new jobs arrive.
- **System tasks** (autoscaler ticks, reconciler, backfills, notifications,
  retention, cost rollups) on the same queue. A small `scheduler` process
  enqueues periodic tasks.
- **Search**: typed B-tree indexes, `pg_trgm` (trigram indexes for fuzzy
  and substring matching) and `tsvector` full-text search.
- **SSE fan-out**: server processes listen on `LISTEN/NOTIFY` channels and
  push events to browsers.

Files live in S3 (MinIO in development). There is no Redis and no Celery.

## Consequences

- Positive: one database to back up, encrypt, fail over and restore.
  Point-in-time recovery covers queue state, entities and audit together.
- Positive: job state and entity writes share a transaction, so a run
  commit is atomic.
- Positive: fewer moving parts for enterprise operators.
- Negative: we write and own the queue (leasing, fencing, retries,
  priorities, quotas) instead of using a ready-made task system.
- Negative: queue churn adds write load and vacuum work to the main
  database. Throughput must be proven by load tests.
- Negative: `NOTIFY` messages are not durable and have a size limit, so
  listeners must treat them as hints and re-read state from tables.
- Negative: Postgres search is less capable than a dedicated search engine.

## Alternatives considered

- **Redis and Celery.** Rejected. They add a second stateful service and
  cannot share a transaction with entity writes.
