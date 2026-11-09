# 0006. Deploy-time plugins compiled to Django apps

- Status: Accepted
- Date: 2026-11-09

## Context

Iki's core defines only base logic and base types. Enterprises add entity
types, fields on other plugins' types, actions, compute environments and UI
components. They want typed, indexed tables, and schema changes they can
review like code. Changing database schema while the app is running, or
giving the app rights to do so, is a security and reliability risk.

## Decision

- A plugin is a git repo of YAML manifests. Enterprises write their own.
  There is no third-party marketplace. Deployed plugins are trusted.
- Plugins are installed, upgraded and removed **only at deploy time**. The
  deployment's config repo holds `plugins.lock.yaml`, with one entry per
  plugin: `{name, git, sha, state: active | detached | purged,
settings_ref}`.
- A CI or image build fetches each plugin by SHA, runs `iki plugin build`
  (a deterministic YAML-to-Django-app compiler), bundles the plugins' Web
  Components, and produces a signed image.
- Migrations are generated when the plugin is **authored**, by `iki plugin
makemigrations`, which runs Django's autodetector and its rename prompts.
  Migration files are committed to the plugin repo and reviewed in PRs.
- A Helm pre-upgrade Job runs `migrate` with a **migrator** database role,
  the only role with DDL rights. The app runs as an **app** role with no
  DDL rights.
- Rollback deploys the previous lockfile and runs reverse migrations. If a
  migration is not reversible, rollback is blocked.
- The build validates schemas and semantics, dependency constraints
  (expressed against entity schema versions, because SHAs cannot be
  ordered), and `apiVersion` compatibility.
- Each plugin declares a settings schema. Values live in the GitOps config
  (ADR 0015) and are checked at build time.

## Consequences

- Positive: real typed tables with indexes and constraints (ADR 0007).
- Positive: every schema change is reviewed and versioned, and the running
  app cannot alter the schema.
- Positive: builds are reproducible from the lockfile.
- Negative: adding or changing a plugin needs a build and a deploy.
- Negative: enterprises need CI to build images. The plugin template ships
  that CI.
- Negative: plugin code runs trusted, so reviewing it is the enterprise's
  responsibility.

## Alternatives considered

- **Installing plugins at runtime, or a third-party marketplace.** Rejected.
  Both require runtime schema changes or running unreviewed code.
