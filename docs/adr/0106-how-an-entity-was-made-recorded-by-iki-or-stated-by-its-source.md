# 0106. How an entity was made: recorded by Iki, or stated by its source

- Status: Accepted
- Date: 2026-11-16

## Context

A run records exactly what Iki did: its action, its inputs at their
revisions, its params, and the environment build it ran in (an image digest
that pins the code and, for a model, the weights). An import records where an
entity came from, as its origin (ADR 0052): the database or file, the record,
its version, a URL, when, and a checksum.

Neither says, in a form people can search, how the thing was made. A
structure predicted in Iki by ImmuneBuilder and one fetched from AlphaFold DB
are both predictions; one fetched from the PDB is an experiment. Today the
first is only findable through its run's action, the second's model is in its
file and nowhere else, and nothing lines the two predictions up. The same
holds for compounds from PubChem and ChEMBL, and sequences from UniProt.

W3C PROV separates what generated an entity from its primary source, and
ModelCIF records a computed model's software and confidence in the file. Both
keep what a system observed apart from what a source asserts.

## Decision

**An action says what software it runs.** `software:` lists each tool by
name and version, with an optional URL: `[{ name: ImmuneBuilder, version:
"1.2" }]`. It's what a person reviewing the plugin checks against its image or
requirements. An action that only fetches or reads files lists none.

**A run says which of its software made each entity.** In `output.json`, a
created entity may carry `made_with`: the names of the action's own software
that made it, when the run chose among them, as an action with a choice of
docking programs would. Naming software the action doesn't list is refused.
Without `made_with`, an entity is made with all of the action's software.

**An origin may say how its source made the entity.** ADR 0052's origin gains
an optional `made`, as the source states it:

- `kind`: `experimental` or `computational`;
- `method`, as the source names it, such as `X-RAY DIFFRACTION`;
- `software`: names and versions, such as AlphaFold 2.3.2;
- `details`: a small object of what else it states, such as a mean pLDDT.

Importers read it from the record (mmCIF's `_exptl` and `_software`,
ModelCIF's `_ma_` categories), never guess it.

**How an entity was made is kept one tool at a time, saying who says so.**
Each is a row of `iki_entity_making`, append-only like origins:

- **recorded by Iki**: made by a run whose action lists software. Its kind is
  computational, its software the run's `made_with`, and the run proves it.
- **stated by its source**: from an origin's `made`, labelled with the source,
  as an agent's own account of its model is (ADR 0072).
- Neither: made by hand, or imported from a source that doesn't say.

An entity found again (its identity matches) keeps each run's record and
each origin's statement, as origins are kept. Bulk imports keep them too,
and bundles carry them: Iki's records as the source deployment kept them,
and origins' statements with their origins.

**People can see and search it.** An entity's page says, above where it was
imported from, how it was made and who says so: one line per run, linking
it, and one per record, linking it. `GET /entities/{ref}/making` lists it,
Iki's records first, and MCP's `get_entity` gives it. Searches filter on it,
one condition matching one row, so "made with AlphaFold, recorded by Iki"
can't match a source's AlphaFold and Iki's ImmuneBuilder.

## Consequences

- "Every structure made with AlphaFold" finds both predictions run in Iki and
  those fetched from AlphaFold DB, and says which is which.
- Iki never presents a source's claim as its own record.
- Plugins' fetches and file imports fill `made` from what their records say.
- Bundles carry `made_with` and origins' `made` as they carry origins.
- Negative: a declared version is the plugin author's statement, reviewed with
  the plugin; the image digest stays the proof of what ran.
