# 0218. First-party plugins and author examples have separate source trees

- Status: Accepted
- Date: 2026-11-25
- Refines: [0039](0039-first-party-plugins-and-the-plugin-template.md),
  [0113](0113-plugins-are-named-in-a-namespace.md),
  [0114](0114-ikis-own-plugins-can-come-from-a-mirror.md)

## Context

Labs need independent operations they can use as shipped. A worked workflow
can also teach an author how to implement a local procedure. Both have a
`kind: Plugin` manifest and both can be tested at a particular commit. A
lockfile author needs to know which source is suitable for a deployment.

## Decision

Iki's first-party catalog consists of the built-in core and the names in
`naming.FIRST_PARTY` with a matching `plugins/<name>/plugin.yaml`. CI checks
the directory and manifest identity against that list. A first-party plugin
serves a lab operation that its target users can adopt as shipped. An
automation that assigns work or changes a record belongs there when its
target labs share that procedure.

Worked recipes live under `examples/plugins/` in a nonreserved namespace.
Each example documents the lab situation it fits, the choices to adapt
and how to test the copy with its dependencies. CI runs those checks
separately from the first-party catalog. The author scaffold remains at
`plugin-template/`. Platform demonstrations belong in test fixtures.

Production config and the default deploy build reject active and detached
pins into the official example, template, fixture and live-check roots,
including at a Git SHA and on configured mirrors. CI and live tools can
pass `--test-only` to build these sources. Local development paths require
the separate `--allow-local` option.

## Consequences

A lab copies a recipe into its own repository, names it in the lab's
namespace, tests it and pins a reviewed commit. Customer namespaces such as
`acme` remain available. Retiring a first-party name removes it from the
active catalog. Its previously recorded tree hashes stay in the additive
history established by ADR 0114.
