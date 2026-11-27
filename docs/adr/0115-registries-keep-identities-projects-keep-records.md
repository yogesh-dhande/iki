# 0115. Registries keep identities, and each project keeps its own record

- Status: Accepted
- Date: 2026-11-17
- Refines: [0007](0007-typed-table-per-type-entity-storage.md),
  [0033](0033-plugin-codegen-and-migrations.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0056](0056-bulk-imports.md),
  [0066](0066-a-run-reads-only-whats-in-its-project.md),
  [0067](0067-a-project-can-keep-its-entities-to-itself.md),
  [0080](0080-triggers-run-actions-when-entities-change.md),
  [0082](0082-unique-fields-link-records-in-other-systems.md),
  [0096](0096-bundles-carry-entities-between-deployments-with-their-history.md)
- Amended: 2026-11-27 by [0165](0165-a-subtype-of-a-registered-type-has-registry-fields-of-its-own.md)
  — a replacement may be of another type under the same identity owner

## Context

An entity was one row with one owner project. A type with an identity (a
protein's sequence, a compound's InChIKey) reused an existing entity on a
duplicate and linked it into the new project (ADR 0035), unless its
project kept its entities to itself, when the new project got a second
entity of its own (ADR 0067). That mixed two things a lab keeps apart:

- **What a thing is.** Aspirin is one compound wherever it's used. Its
  structure, formula and weight follow from that, and so do the facts a
  registration team curates: its CAS number, its INN, its hazard class.
  It should have one ID, CPD-000123, which every report and notebook can
  cite.
- **What a project knows about it.** Its name in this project, notes,
  assay results, the organism a sequence came from. Two projects' data
  about one compound differ, and each is the project's own.

With one row for both, a second project could read the first's notes once
the entity was linked, but couldn't change them. A restricted project's
copy was a second CPD with the same structure. Facts had nowhere to live
that every holder would see. And `on_duplicate: allow` or `warn` let one
project hold the same compound twice.

Registration systems in pharma (Dotmatics Register, CDD Vault, Benchling's
registry) split these, as do public ones (PubChem's substance and compound
records). A registration team curates the registry; projects hold their
own data about what's registered, under GxP and 21 CFR Part 11
expectations: every value attributable, and nothing silently overwritten.

## Decision

### Registries, entries and records

**A registry entry is an identity, with one citable ID.** It holds its
type's identity fields, the fields derived purely from them at
registration (a formula, a weight, a canonical form) and the type's
registry facts. Its ID is the type's human-readable ID, such as
CPD-000123. It belongs to no project. Identity and derived fields never
change once set. Normalisation (salts, tautomers, cleaning up a sequence)
happens once, when the entry is registered, as the plugin's registering
action does it.

**A project record is a project's data about an entry, one per project
and entry.** It holds the type's other fields (a name, notes, an organism,
results), with revisions, provenance and archiving as ADR 0035 describes.
It's also the project's holding of the entry: who added it there, and
when. People see the entry's ID; "record" is a word for the docs and the
API.

**A registry is the identity boundary.** Every project is in exactly one.
Each deployment has a default registry, which every team's projects may
use. Org Admins add more, for a CRO's clients or a partner program, each
naming the teams whose projects may use it. A project's registry is
chosen when it's created, from those its team may use, and an Org Admin
can change it until it has entities; after that it's fixed. Each change is
audited as `project.registry_changed`.

**Types without an identity are project data only.** A view, a
collection, a batch or a run's result is a record with no entry, as every
entity was before. Physical things (batches, lots, samples, tubes) are
types of their own that refer to the identity; a type that wants several
of one compound in a project models them so, since a project now holds at
most one record of each entry. `on_duplicate` is removed from the spec:
`allow` and `warn` made exactly that second record.

### Registering

**Registering an identity that's already in the registry returns its
entry, and a new record for your project.** `POST /entities` with a type
that has an identity looks the identity's hash up in the project's
registry, under a lock on the hash:

- not there: a new entry is registered, with a new ID, and the project
  gets its first record of it (201, `registered: true`);
- there, and the project has no record of it: the project gets a new
  record, with only the values the caller gave (201, `registered: false`);
- there, and the project has a record of it already: that record is
  returned unchanged (200, `reused: true`). An archived one says so, and
  must be restored to change.

The second case discloses that the identity exists, and its ID. It never
discloses who registered it, when, which projects hold it or anything any
of them recorded. That's the price of one ID per identity. A partner or
client that mustn't learn even that gets a registry of its own, which
also keeps its IDs from being cited by anyone else's projects.

Derived values and facts given with a registration of an existing entry
fill what the entry hasn't got yet. A fact that differs from the entry's is
refused, as facts change only through a registrar; the refusal doesn't say
what the entry's value is. A derived value that differs is the entry's
already, computed from the same identity (another version of the software
may round it otherwise), so the entry's stays and the registration goes
ahead. A person editing a record can't change a derived value the entry
has: that's refused.

A registration of an identity whose entry a registrar superseded (below)
is refused, naming the entry that replaced it and why.

### Storage

**An entry is an `iki_entity` row with a registry and no project.** Its
type's entry table (`iki__small_molecule_compound__entry`, generated with
the type's own) holds its identity, derived and fact fields, with history
as any type table has. A record is an `iki_entity` row in its project, as
before, whose type tables hold the other fields. A record of an identity
type names its entry and carries the entry's ID. The database holds the
rules:

- an entry has a registry and no project; a record has a project;
- an identity is registered once per registry;
- a project holds one record of an entry;
- an ID is unique among entries and records without an entry.

Entries are entities so that references, identity hashes and history work
the same way for both. Entry changes (a derived value filled in, a fact
set, a supersession) are numbered revisions of the entry, append-only like
a record's.

**References point at entries, never at another project's record.** A
reference to a type with an identity holds the entry: `compound` on a
batch, `heavy_chain` on an antibody, a collection's members, a view's
targets. A record's reference resolves, for each reader, to what they can
read of that entry. An identity that includes a reference (an antibody's
chains) hashes the entries' IDs, so the antibody is the same one in every
project. A reference may name the entry by its ID, its UUID, or the UUID
of a record of it the caller may use; it's stored as the entry. To refer
to an entry, the record's project must hold it: its own record, or one
shared into it. Otherwise the reference is refused: "CPD-000123 isn't in
Atlas: add it to the project first". Fields in an entry may refer only to
other entries.

A view pins revisions of records (ADR 0110): a target that's an entry is
the view's project's record of it, at that revision.

### Reading

**An entry is visible only to projects that hold a record of it.** Its ID
alone grants nothing: naming CPD-000123 that you can't read answers 404,
as anything you can't read does. There's no browsing or substructure
search across a registry; projects search their own records, whose
searches can name entry fields as their own. A per-registry setting that
lets members browse may come later.

Holders see an entry's values, its ID and its registry, and that it was
superseded. Its history shows them what registrars changed (who, when and
why) and what was set through the projects they can read; of anything else,
only what was set, never by whom or when.

**The API addresses a record by its UUID, or by its ID and project.**
`GET /entities/CPD-000123?project=atlas` is Atlas's record. Without
`project`, the ID names the one record you can read; when you can read
several, the answer is 409 (`choose_project`), listing their projects.
The web app's entity page does the same with a project switcher, and its
links carry the project. `GET /entries/CPD-000123` is the entry, with the
records of it you can read.

### Facts and registrars

**A plugin marks each field of an identity type as registry or record
data.** Identity fields are always the entry's. `registry: derived` marks
a field derived from the identity, and `registry: fact` a registry fact.
Every other field is a record's. A type with an identity declares its
identity fields itself; an extension of it may mark its own fields too
(the antibody plugin's numbering of a protein is derived).

**Registry facts are curated facts that the identity doesn't determine.**
An INN, a CAS number, a development code, a stereochemistry comment, a
controlled-substance or hazard class, a biosafety class, a curated UniProt
cross-reference. They're never free text: the spec refuses a fact of type
`text` or `json`, and a string fact needs a pattern or a format. A fact is
never required or computed. A `unique` fact is unique within its registry,
as a record's unique field is within its project (ADR 0082); since 2026-11-27 it's
a one-field `unique` rule `within: registry`, and a rule may hold several facts
together that way. Organism is a
record field: the same sequence occurs in several species.

- Anyone who may edit a record of the entry may set a fact while it's
  empty, by editing the record or registering with it; a run may too.
- After that, only a **registrar** changes it, with a reason, through
  `PATCH /entries/{id}`. Each change is a revision of the entry, audited
  as `registry.facts_changed`. A signature (step 55) will be able to go
  with it; until then the reason and the revision are the record.

**Registrars are given per registry by Org Admins.** A registrar curates
one registry: its facts, and superseding its mistaken entries. They see
every entry in it, with how many projects hold each, but no project's
records or which projects hold them. Org Admins aren't registrars by
virtue of their role, as they read no project data by it either
(ADR 0024). Each appointment is audited. A registry of a client's may have
the client's own registrars. Registrar decisions are a person's: there
are no MCP tools for them.

### Superseding

**A registrar supersedes a mistaken entry with another.** CPD-000123 →
CPD-000456, with a reason: two registrations that normalisation should
have made one, or a structure registered wrongly. The superseded entry
keeps its ID and values, is marked with what replaced it, and can't be
registered again. The replacement must be a live entry of the same type
in the same registry. A supersession can't be undone. (Amended 2026-11-27,
ADR 0165: or of another type under the same identity owner, which a
registrar may register for it, and a record moved to one of another type
is a new record, the old one archived.)

**Holders are told, and move their records themselves.** Each holding
project's Admins, and whoever added its record, get a notification, and
the project's webhooks hear `entry.superseded`. The record, notification and
webhook identify the replacement by its registry HRID. The record and
webhook also carry its entry UUID. The replacement's facts and project
records follow their own read permissions. Each notification is visible and
eligible for email while its recipient can read the holding project. An
Editor moves the record with
`POST /entities/{id}/move`: the record (its data, history and runs) now
holds the replacement, and the project's own records that refer to the old
entry refer to the new one, each as a revision giving the reason. That's a
correction, so it changes immutable references too: a batch registered
against the mistaken compound is a batch of the corrected one, as moving a
lot to its corrected parent is in a registration system. Each referring
record is otherwise saved as any edit is: its rules are checked, a rule that
holds it holds it with the move's reason, one that refuses refuses the whole
move, and a check that read the old reference is cleared and runs again.
Archived records, and those of detached plugins, keep their references. A project that holds
the replacement already can't move the old record onto it; it archives one
of the two. Each move is audited as `entity.moved`.

### Sharing, runs and triggers

**Sharing data is sharing a record.** Linking a record into another
project (ADR 0035) lets that project read the record's data, governed by
the sharing setting of ADR 0067, which now governs records. Adding an
entry to a project (`POST /entries/{id}/records`) gives the project a new,
empty record of its own: the identity goes, none of the source project's
data. It needs the entry to be readable through a project that allows
sharing, and both projects in one registry. A reference from one registry
to another's entry is refused.

**Runs still read only their own project** (ADR 0066): their inputs are
the project's records, its own or shared into it. What a run creates of an
identity type registers in the run's project's registry as `POST
/entities` does, bulk imports (ADR 0056) and bundles (ADR 0096) included.
A bulk import counts the entries it registered, in place of the entities it
linked. A run's values for an entry's fields follow the same rules as a
person's: set while empty, refused for a fact that differs. A run's effect
on a record keeps the entry revisions it made too, so an entry's value
names the run that set it, for readers its revision is disclosed to who can
read the run's project, as a record's own values do. A revision a run made
with no run recorded here, as an imported one, says so, rather than that
its run is hidden.

**A bundle carries the entries its records are of, without their
history.** An entry's history is its registry's, not the exporting
project's, so a bundle's `entries.ndjson` has each entry's UUID, ID
and values as they are, and each record names its entry. Importing finds
each identity in the importing project's registry: one registered there is
the entry its record then holds (its empty facts are filled; a differing
fact, a superseded entry or another type there is a conflict in the plan),
and one that isn't is registered by the import's applier, keeping its ID
and HRID when they're free there. References to a bundle's entry name the
entry here.

**Triggers can fire on registration.** `on: entity.registered` fires when
a record's creation registered its entry. The first-party plugins compute
derived values on it (a compound's descriptors, a protein's properties),
so a second project's record of a compound doesn't compute them again.
`entity.created` still fires for every new record.

## Consequences

- One ID per identity in a registry: reports, notebooks and partners cite
  CPD-000123, and every project's data about it is found under it.
- Each project's data is its own and changeable by its own Editors. A
  second project no longer reads, or is blocked by, the first's notes.
- Facts are curated once, by registrars, and every holder sees the same
  value, with who changed it and why.
- Within a registry, any Editor can learn that an identity is registered,
  and its ID, by registering it. A registry of its own is how a client or
  partner avoids that; Org Admins decide which teams use it.
- A project holds one record per entry. A type that needs several things
  of one identity models them as their own type (a batch, a sample).
- Moving a record after a supersession rewrites references in the moving
  project, as revisions with a reason, including immutable ones. Other
  projects move their own.
- Every query on entities has to say whether it wants entries or records.
  `access` decides what's readable, and entries are never in a project's
  list or search.
- Plugins mark their fields (`registry: derived`, `registry: fact`), and
  their migrations make entry tables. Pre-M1, the first-party plugins'
  migrations were regenerated.

## Alternatives considered

- **Keep one entity, linked into projects (ADR 0035).** Rejected: one
  project's data would stay readable, and unchangeable, by every project
  it was linked into, and facts would belong to whichever project
  registered first.
- **Copy identity values into every record.** Rejected: facts would have
  to be rewritten in every record when a registrar changed one, and the
  identity would no longer have one place.
- **Entries in a table of their own, outside `iki_entity`.** Rejected: a
  reference to any entity (a collection's members, a view's targets)
  couldn't be one foreign key, and entries would need their own history.
- **Registry-wide disclosure checks (refuse a registration that would
  reveal another project's identity, as ADR 0067 did).** Rejected: it
  gives one identity several IDs, and isolation that strong is what a
  separate registry is for.
- **Registrars as an organization-wide role.** Rejected: a client's
  registry would be curated, and its identities seen, by every registrar
  in the organization.
- **Let a registration of a superseded identity return its replacement.**
  Rejected: when the supersession corrected a wrongly registered
  structure, that would hand a real, different compound the wrong ID.
  Refusing names the replacement, and the person decides.
