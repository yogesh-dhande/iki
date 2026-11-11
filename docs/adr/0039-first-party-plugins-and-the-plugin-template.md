# 0039. First-party plugins live in this repository; labs start theirs from a template

- Status: Accepted
- Date: 2026-11-11
- Refines: [0006](0006-deploy-time-plugins-as-django-apps.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md)

## Context

Iki ships plugins of its own for the common kinds of entity (DNA, proteins,
structures, antibodies, small molecules). Labs write plugins for their own
types and add fields to Iki's. Both are installed the same way: a
deployment's lockfile pins each plugin to a git commit (ADR 0034).

First-party plugins change with Iki. A new core type, a new field kind or
a new action runtime is easiest to land with the plugins that use it, in one
reviewed change, tested together. A lab's plugin has its own repository,
reviews and CI. Most labs start with no plugin experience, and pharma labs
need every schema change to go through review.

## Decision

**First-party plugins live under `plugins/` in this repository.**

- Each one is a directory in the same form as any plugin: `plugin.yaml`,
  `types/` and `migrations/`.
- Each has its own version.
- A deployment pins one like any plugin, by this repository
  (`https://github.com/ikisci/iki.git`), its `path` (`plugins/dna`) and a
  commit. The dev cluster installs them from their directories.
- Each can also be published as a repository of its own later, without
  changing its manifests.

`dna` is the first. It holds `dna.DNASequence`, whose identity is its bases
and topology, with its features as GenBank lists them, and `dna.Primer`.
Both extend the new abstract `core.Sequence`, which Iki's other sequence
types will extend too, so one search finds every kind of sequence.

**`plugin-template/` is where a lab's plugin starts.** It holds:

- an example entity type with references to its own type and to another
  plugin's
- an extension adding fields to `dna.DNASequence`
- a setting
- committed migrations
- a CI workflow that validates the plugin against what it depends on and
  fails when its migrations don't match its manifests

Its README walks through renaming it, changing it and deploying it. The
workflow pins its actions by commit and starts with no token permissions,
like Iki's own. Iki's CI lints it with the same tools.

**Both are tested here as a deployment would use them.** Every first-party
plugin, and the template, must:

- validate with the plugins it depends on
- have migrations that match its manifests
- build, migrate a database forwards and back, and store entities

As the action runtime arrives (commits 20 to 23), the template gains an
example action for each compute tier, a trigger, a component and fixture
tests, and the dna plugin gains its actions.

## Consequences

- One pull request can change the core, a first-party plugin and the
  template together, and CI checks them together.
- Labs pin first-party plugins by a commit of this repository. Since a
  commit pins every first-party plugin, the lockfile's `path` says which one
  it installs.
- The template's CI installs the `iki` CLI from PyPI at the deployment's
  Iki version, so it works once Iki is published there (commit 50).
- The `features` of a DNA sequence are JSON, checked by a JSON Schema, not a
  table of their own. List items can't be objects yet. A feature table can
  come with a later version of the spec, as a migration.
