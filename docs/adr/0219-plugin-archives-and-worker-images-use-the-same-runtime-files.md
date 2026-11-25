# 0219. Plugin archives and worker images use the same runtime files

- Status: Accepted
- Date: 2026-11-25
- Refines [0059](0059-workers-fetch-plugin-code-and-build-environments.md) and
  [0218](0218-first-party-plugins-and-author-examples.md).

## Context

A self-hosted worker downloads a plugin archive from the server. A backend
worker starts with the plugin staged in its image. An action needs the same
code, environment definitions and reference data in both places. A plugin
checkout also includes the files used to teach, build and test it.

## Decision

Both distributions select files from the same prepared plugin tree. The
build first resolves the plugin's pinned WebAssembly modules and their
notices. It keeps manifests, migrations, executable code, environment
requirements and setup scripts, bundled components and runtime resources
at their plugin-relative paths. A lab can choose the names of its own
helper modules and reference dictionaries.

The root `tests/` and `examples/` directories hold fixtures and teaching
materials. Author tools, CI metadata and working-copy caches also stay in
the checkout. Iki's own image and component build inputs stay with their
sources. Distributed files retain their provenance and redistribution
notices. The archive digest describes the bytes available to workers.
The first-party tree hash continues to cover the whole Git tree.

## Consequences

Plugin authors keep files their actions read at runtime outside the
repository-only directories. The test runner reads fixtures from source.
Dev seeds, documentation downloads and worked tutorials use their
source-controlled inputs directly. An archive-content test compares the
files and bytes in the server's archive with the staged worker files.
