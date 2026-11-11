# 0038. The core plugin ships as an app in the server, starting with collections

- Status: Accepted
- Date: 2026-11-11
- Refines: [0033](0033-plugin-codegen-and-migrations.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0037](0037-entity-search.md)

## Context

Scientists work on sets of entities: the hits of a screen, the structures
for a paper, every human kinase over 50 kDa. A set has to be named, shared,
cited and reused. Actions will produce sets (a similarity search's results)
and workflows will fan out over them. Some sets are fixed lists, picked by
hand or frozen for a run. Others are a standing question whose answer grows
as entities are registered.

A set is a thing scientists refer to, so it should be an entity like any
other, with an HRID, revisions, provenance and project links. Until now the
core plugin only held the abstract `core.Entity`, the server's own model,
so nothing compiled it.

## Decision

**`core.Collection` is a core type.** A collection has a `name` and a
`description`. It has either `members`, a list of up to 10,000 references to
entities of any types (a static collection), or a `search`, as
`POST /entities/search` takes it without paging (a dynamic collection). It
can't have both. Freezing a dynamic collection is an edit that sets its
members to what the search finds now and clears its search, so its revisions
record both.

**A collection never shows anyone an entity they can't read.**

- A dynamic collection's search is checked as the person who saves it. A
  project it names must be one they can read.
- It runs as whoever reads the collection, so two readers can see different
  members. Searching with a `collection` gives a collection's members. Only
  members the caller can read come back, through the same filters, sorting
  and keyset pages as any search.
- A search that stops working because a type or field it names is gone
  answers 422 and says why. It isn't treated as empty.

**Core types can have rules beyond their manifests.** A manifest checks
each field on its own. The server's entity services also run rules for
core types (`iki_server.entities.rules`) on every create and edit, so they
hold for the web, the API, MCP and runs alike. Subtypes follow their
parents' rules. On an edit, a rule runs only when a field it reads changes,
so renaming a collection doesn't re-check its search. Plugins can't add
rules; their checks are their manifests'.

**The core plugin's app ships in the server.** `make core` generates
`server/src/iki_plugin_core` from `spec/src/iki_spec/core`, exactly as
`iki plugin build` does for any plugin. It then makes its migrations with
the same history rules (ADR 0033). The server's package ships the app, and
its settings install it before the deployment's plugins. A test fails when
the app or its migrations fall behind the manifests. The core plugin
releases with Iki (ADR 0034), so its migrations are Iki's own. Other
plugins can refer to and extend its types without declaring a dependency.
The generated `core.Entity` stays the server's own model.

## Consequences

- Collections can be searched, linked into projects, referenced by other
  entities, and audited like any entity. Later actions can take them as
  inputs and produce them as outputs.
- A static collection is capped at 10,000 members, since each revision
  records its whole list. Larger sets are dynamic collections, or datasets.
- A run over a dynamic collection must pin the members it ran on. Runs
  record their inputs' revisions; the run model (commit 21) records the
  members too.
- `core.View` and `core.App` will be core types the same way.
