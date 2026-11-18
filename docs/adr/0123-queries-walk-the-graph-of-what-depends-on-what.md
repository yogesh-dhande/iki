# 0123. Queries walk the graph of what depends on what

- Status: Accepted
- Date: 2026-11-18
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0037](0037-entity-search.md),
  [0066](0066-a-run-reads-only-whats-in-its-project.md),
  [0067](0067-a-project-can-keep-its-entities-to-itself.md),
  [0105](0105-an-input-can-include-what-its-references-name.md),
  [0110](0110-a-view-shows-several-entities-each-at-its-revision.md),
  [0115](0115-registries-keep-identities-projects-keep-records.md)

## Context

Iki recorded what depends on what in several places: a ref field names
an entity, a run's lineage says what each entity it made was made from
(ADR 0035), what its inputs included (ADR 0105), which entities it
updated, and which files it was given. People could see one step of it on
an entity's page and a few generations of runs in a picture. They
couldn't ask the questions a lab asks of it: when a donor withdraws
consent, what was made from their samples, in every project; when a lot
of reagent is recalled, what it touched; before correcting a result,
what's downstream of it; for an audit, everything a released result
depends on. Benchling answers with lineage on each entity, Dotmatics with
"where used", W3C PROV models it as `wasDerivedFrom`, and GitHub's
dependency graph shows a repository's dependents and dependencies, each
only as far as you can read.

## Decision

**The graph's edges are one table**, `iki_graph_edge`: a node depends on
another (`on`), by one of four kinds of edge.

- `reference`: a ref field, single or in a list, of a record or of a
  registry entry, names it. Refs to an identity name its entry (ADR 0115).
- `run`: a run made it from one of its inputs, or from what an input
  included (`antibody.heavy_chain`), or updated it while reading another
  input. The edge names the run and the revision it read.
- `file`: a run made it from a file param, and the file was held by an
  entity or written by a run (an upload or an artifact) on one. The edge
  names the file.
- `record`: a project's record and its registry entry, both ways. They're
  one thing, so walking between them costs no depth.

Nodes are entities: records and entries. Files and runs are on the edges,
as how a step was taken, because what people ask about is entities.

**Triggers keep the edges, whatever writes.** Statement-level triggers
with transition tables, so a bulk import's million rows make their edges
in one statement, on `iki_lineage`, `iki_run_effect` and `iki_entity`
(records' entries), made by `iki_graph`'s migration, which fills in what
was already there; and on every plugin table with ref fields and every
list of refs, installed by `sync_plugins` as it installs unique and lock
triggers, and filled in when a table's references change. A field that's
removed or a plugin that's purged takes its edges with it. The functions
are `SECURITY DEFINER` with a pinned `search_path`, and the app's role
can't write the table: only the triggers do. A reference edge is what the
field holds now; run and file edges are history, which never changes.

**`upstream(X)` and `downstream(X)` are recursive CTEs** over the edges,
with a depth (default 10, at most 30), kinds of edge to follow
(`reference`, `run`, `file`; records are always followed), and types and
archived state to narrow the results, not the walk. Each node is kept
once per depth it's reached at (`UNION`), so diamonds and cycles cost the
nodes times the depth at most; record edges, which cost nothing, can be
taken twice in a row (record, entry, another project's record) and no
more. A walk is materialised into a temporary table for its transaction
and runs inside search's 15-second limit; past it, it answers
`search_too_slow`, as search does. Results are records (entries are
walked through), nearest first then by HRID, with keyset paging, a total,
and `beyond` when there's more past the depth.

**Each result says why it's there.** Its `path` is the first way the walk
reached it, from the root: each step's kind, field or input, run,
revision and file. A step back always goes to a node reached nearer, so a
path never loops. Steps between a record and its entry are folded away: a
reference to an entry reaches the record people read, and a step to
another project's record of the same entry is a `record` step.

**Only what you can read is walked.** An edge is the data of the entity
that states it: a reference is its holder's, a run's edge is the made
entity's provenance, a record's link to its entry is the record's.

- Downstream, the walk only goes to entities you can read. What refers to
  X from a project you can't read is that project's data: it isn't named,
  counted or walked through, as GitHub lists only the dependents you can
  see.
- Upstream, an entity you can read may depend on one you can't: its
  reference or its run's input is visible to you, as a run's unreadable
  input is (ADR 0066). The walk stops there; the result counts it in
  `hidden` ("3 trails lead to entities you can't read: ask someone who
  can"), never naming it, and never goes past it, so nothing beyond leaks.
- Record edges go only to records you can read, so how many projects hold
  an entry never shows. A view (ADR 0110) is readable only with all it
  shows, as everywhere else; a private project's entities (ADR 0067) are
  readable only by its members. Runs are named only if you read their
  project, and files only if you can read them. What a plugin's purge
  tombstoned (ADR 0092) is readable by nobody, and its edges, kept with
  the rows they come from, aren't counted as references to it.

Readability is checked in the walk's SQL, from the projects you read,
with every literal checked to be a UUID, a type's name or a number, so
nothing a caller sends is in it.

**Every surface has it.**

- Search: `upstream` and `downstream` are fields of every entity, with
  `of` (an ID or HRID, or `{entity, depth, edges}`) and `is_set`, so they
  combine with any condition: "every Sample downstream of DNR-000001 not
  yet released". An identity's HRID starts at its entry, so it covers
  every project's record you read. A search may have three such
  conditions. In the web app's search box, `downstream:DNR-000001` and
  `upstream:RES-000004` say it.
- The API: `GET /entities/{ref}/upstream` and `…/downstream`, paged, and
  each `.csv` for everything at once (at most 100,000 rows, with each
  path as text and spreadsheet formulas escaped); `GET
/entities/{ref}/graph` for a picture, a few steps each way with the edges
  between, capped at 200 entities and 500 edges. These replace
  `/entities/{ref}/lineage` and `/lineage/graph`.
- MCP: read-only `get_lineage` (upstream) and `get_impact` (downstream).
- Both SDKs: `upstream` and `downstream`, and their CSVs.
- The web app: each entity's **Lineage** page has Upstream and
  Downstream, as a tree (an ARIA tree you can walk with the keyboard), a
  list (the impact list, with why each is there, filters and export) or a
  graph.

**Triggers walk it too.** A trigger can give its action's other inputs
what's related to the entity that fired it (`related: {aliquots:
{direction: downstream}}`), so "when a donor's consent is withdrawn, hold
everything derived from them" is one trigger, as Benchling's and
Dotmatics' lineage queries feed their workflows.

- Each such input gets the records of its type in the trigger's project
  that are that way of the entity, as far and through what the trigger
  says, not archived. The walk runs as the Admin who turned the trigger on
  (ADR 0080), whose authority the run has, and reads only the project's
  records: a trigger turned on in one project never reaches another's,
  even through an identity's entry.
- All of them or none. A hold or a recall that reached only some would
  look done when it isn't, so past the most an input takes (its `max`, and
  never more than 1,000) or past search's time limit the firing fails,
  saying which, and starts no run; the trigger stays on. A required input
  with nothing related skips the run. The Admin is checked before the
  walk, as before a launch, so one who can no longer run it walks nothing.
- The firing keeps how many each input was given, the run's reason says
  it, and the triggers page says what each trigger gives its runs before
  it's turned on.

## Consequences

- Every write of a reference pays for an edge row and its indexes; bulk
  writes pay once per statement. Reads of the graph are index scans,
  tested on a synthetic graph of 100,000 entities.
- Lineage is no longer only runs: an entity's page shows what refers to
  it, and impact analysis covers both.
- A reference edge names what's there now, not a revision. What a record
  depended on at a past revision is in its history (ADR 0035); the graph
  answers what depends on what now, and what runs read then.
- What you can't read downstream of something isn't in your answer, so
  an impact list is as complete as what you can read. A recall or consent
  withdrawal across every project is for someone who reads them all; a
  trigger covers its own project, and is turned on in each.
- A trigger's run is given a list of entities fixed when it fired. What's
  derived later isn't in it; the trigger fires again only when the entity
  changes again.
