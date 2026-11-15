# 0090. Apps are compositions of Iki's components

- Status: Accepted
- Date: 2026-11-15
- Refines: [0084](0084-plugin-components-run-in-sandboxed-frames.md),
  [0085](0085-types-configure-ikis-own-viewers.md)

## Context

Labs want pages of their own: a project's dashboard of this week's
constructs and their QC, a screen that lists a campaign's proteins and shows
the one picked in the structure viewer, a tab on an antibody with its
variants side by side. Each lab wants a different one, and they change as
the work does. Agents are good at building such pages, if what they build
can be checked before anyone opens it.

Retool, Appsmith and Grafana let people compose pages from components
bound to each other by small expressions (`{{ table1.selectedRow }}`).
Their components can also run arbitrary JavaScript and call any API, which
is why they're a common way for data to leave a company. In Iki, a page
anyone can write must not be able to do more than the person viewing it
could do by hand, nor send what they see anywhere.

## Decision

**An app is an entity of the core plugin, `core.App`.** It has a name, a
description and a spec. Being an entity, it has a project, permissions,
history and search, and each save is a revision, as with views (ADR 0086).
An app shows what the person viewing it may read, not what its author
could: it holds no data and no credentials of its own.

**The spec is data.** It lists panels, each one of Iki's components with
its props, and how wide it is on a twelve-column grid, as Grafana lays out
dashboards. A panel has an ID that later panels refer to. The spec's JSON
Schema (`app/v1`) and the server check it on every save. Each component
names the props it takes and their schemas, and what it offers later panels
(a grid's `selection`, say). A panel may refer only to panels before it, so
the page can't loop. A spec that doesn't check isn't saved, and the error
says which panel and prop.

**Components are Iki's own.** Those that come first:

- `iki.entities`, a grid of the entities a search finds, with the person's
  permissions: a type, filters as the search API takes them, and columns,
  each a field or an expression. It offers the rows it shows and the ones
  picked.
- `iki.viewer`, one of Iki's viewers (ADR 0085) on an entity.
- `iki.text`, text with expressions in it.

**An app can be about an entity.** A spec that says `"entity": {"type":
"protein.Protein"}` is a tab on the pages of that type's entities (and its
subtypes'), as Salesforce's record pages and Retool's URL parameters give a
page its record. Its expressions read the entity it's on as `entity`, and a
grid's condition can take its value from one expression
(`{"field": "organism", "op": "eq", "value": "{{ entity.values.organism
}}"}`), as it can from a panel before it, which makes master and detail
grids. An app is a tab only on the entities of its own projects, which their
Editors made it for, and only for people who can read it: someone else's
project can't add tabs to yours. The tab opens only for apps the server
lists for that entity.

**A project's page can open with an app.** Its Admins choose one of the
project's apps, not one about an entity, as GitHub shows a repository's
README and Benchling a project's dashboard. Everyone who can read the
project sees it above the project's summary, while they can read the app
there: archived, or taken out of the project, it isn't shown, though it
stays chosen until an Admin changes it. Choosing and removing it are in
the audit log (`project.dashboard_set`, `project.dashboard_cleared`), with
the app it replaced. The choice is kept beside the project, not in it, so
projects don't depend on entities.

Plugin components in sandboxed frames (ADR 0084), charts, and a run
launcher come next, each with what it needs checked the same way. No
component runs code from the spec.

**Expressions are CEL.** A prop's value can be an expression between
`{{` and `}}`, as in Retool and Iki's own display titles: the whole value
(`{{ proteins.selection[0] }}`, an entity) or parts of text (`{{
size(proteins.selection) }} picked`). The language is Google's Common
Expression Language, which Kubernetes, Firebase and Envoy use for the same
reason: it's pure, has no loops or I/O, always ends, and is typed. It can
read only what the page gives it: the app's inputs, what earlier panels
offer, and, in a column, the row. What it makes is shown as text, never
HTML: text as it is, numbers as written, yes or no, a list's items with
commas between, and an entity by its title. A prop that is one expression
and nothing else, as a viewer's entity is, takes that expression's value
as it is. A panel waits for the panels before it to offer what they have,
so it never reads a grid that hasn't loaded.

**One implementation, in Rust.** The browser evaluates expressions with
iki-wasm (ADR 0088), which now includes the `cel` crate (MIT), in a Web Worker,
as it reads files. The server checks them when an app is saved, with the
same module run by wasmtime, so what the server accepts is exactly what
the browser runs, and the desktop app can use the crate itself. iki-wasm
also splits text into its expressions, minding CEL's strings and maps
(`{{ {"a": {"b": 1}} }}`), so both split it the same way, and writes a
failure plainly, naming the kinds of values involved but never what's in
them. An
expression is at most 1,000 characters and nests at most 32 deep; the
server gives reading each one a budget of instructions, and the browser
stops a rendering that runs for longer than a second. The browser's worker
renders once before it's used, since some browsers run a module's first
calls slowly, and a cold start shouldn't count against an app.

## Consequences

- An app can't show anything its viewer couldn't find, nor write anything,
  nor send data anywhere: it has no network of its own, no links out and
  no HTML. Writes, runs and downloads come with the components that do
  them, through Iki's own screens and their confirmations.
- New kinds of panel are code in Iki or a plugin, reviewed as any code is,
  never something a spec can add.
- iki-wasm grows from about 45 KB to about 1 MB (about 300 KB as sent),
  loaded once, when a page reads a file or shows an app.
- The server image carries wasmtime, to run iki-wasm for its checks.
- Agents write apps with three MCP tools over two endpoints:
  `describe_apps` (`GET /apps/guide`: the schema, each component's props
  and what it offers, how expressions work, and an example), `check_app`
  (`POST /apps/check`: every problem saving would refuse, each with its
  panel and prop, nothing saved) and `create_app`, which answers with the
  page to open. Changing one is `update_entity`, checked the same way.
- An app opens on its own page, as a tab on the page of an entity it's
  about, or as its project's dashboard
  (`GET`/`PUT`/`DELETE /projects/{id}/dashboard`). Drafts to preview before
  saving follow.
