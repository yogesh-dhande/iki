# 0016. Versioned specs, codemods, deprecation policy and LTS lines

- Status: Accepted
- Date: 2026-11-09

## Context

Enterprises build their own plugins against Iki's specs and run their own
deployments. Many cannot upgrade on Iki's schedule because of validation
requirements, change freezes or limited staff. They still need security
fixes. Plugin authors need to know which spec a manifest targets and how to
move to the next one.

## Decision

- Every spec carries a version: `apiVersion: iki/v1`. The JSON Schemas live
  in `spec/` and are published at URLs such as
  `https://ikisci.org/schemas/plugin/v1`.
- The build checks each plugin's `apiVersion` against the platform.
- Plugin dependency constraints are expressed against entity **schema
  versions**, not git SHAs, because SHAs cannot be ordered.
- `iki plugin upgrade` applies **codemods**: automated rewrites that move a
  plugin's manifests to a newer spec version.
- Deprecated features keep working for **one minor release** before
  removal.
- Iki publishes **LTS (long-term support) lines** that receive security
  backports. Enterprises can delay upgrades and still get patches.
- The worker protocol supports version N-1, so workers and servers can be
  upgraded independently (ADR 0010).

## Consequences

- Positive: enterprises can plan upgrades and stay patched in between.
- Positive: plugin authors get a mechanical upgrade path instead of reading
  release notes and editing by hand.
- Positive: published schemas let editors and CI validate manifests
  offline.
- Negative: maintaining LTS branches and backporting fixes is ongoing
  work.
- Negative: a one-minor-release deprecation window is short. Codemods and
  migration tests must be reliable for it to work.
- Negative: every spec change needs a compatibility decision and, often,
  a codemod.
