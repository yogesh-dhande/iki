# 0166. A salt's ratio is stated or follows from charge balance

- Status: Accepted
- Date: 2026-11-19
- Refines: [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md)

## Context

A compound batch records its salt and how many of it there are for each of
its compound (`salt_equivalents`), and its formula weight follows from them.
When a structure shows its salt, the ratio is counted from the drawing. When
only a field says it, as a supplier's SDF `Salt` column does, the ratio was
taken as one for each parent unless a `Salt_eq` field said otherwise.

That's the convention for a hydrochloride or a sodium salt, but not for a
metal of charge 2. Calcium takes the place of two hydrogens, so one calcium
for each acetic acid takes two hydrogens from a molecule that has one:
98.114 g/mol for a material whose formula weight is 79.083 (calcium acetate,
Ca(C₂H₃O₂)₂, PubChem CID 6116, is 158.17 g/mol for two acetates). A dose
weighed out by it is 24% short. Atorvastatin calcium, esomeprazole magnesium
and zinc gluconate are all two to one.

A `Salt` field also says what the material is, so that a record's name, CAS
number and PubChem CID are the salt's, not its free acid's. It was read only
when the run made batches, so the same file imported without them gave
aspirin the name, CAS number and CID of aspirin sodium.

## Decision

**A salt's ratio is the one stated, or the one charge balance settles, or
none.** With no equivalents given:

- an acid's or base's salt, and a metal of charge 1's, is one for each parent,
  as before: the convention a hydrochloride and a sodium salt are named by;
- a metal of charge 2 (calcium, magnesium, zinc) is 1/2 for each parent when
  the parent has exactly one acid group whose hydrogen it can take (a
  carboxylic acid, a sulfonic or sulfuric acid, each OH of a phosphorus acid,
  or a tetrazole), the only ratio that balances. The run says so;
- otherwise, two acids (folinic acid, whose calcium salt is one to one but
  could be two to one) or none Iki counts (esomeprazole's, a benzimidazole's
  NH), the ratio isn't known. The batch records its salt without equivalents,
  it has no formula weight, and the run says to give `Salt_eq`.

Recomputing a batch's formula weight follows the same rule, so a batch whose
equivalents are blank gets the weight charge balance gives, or none.

**A `Salt` field says what the material is, with batches or without.** It's
read either way, so a record whose Salt field makes it a salt doesn't give
its compound the salt's name and identifiers, and it's held to agree with
the structure either way. A Salt field that says there's none (`N/A`,
`none`, `free base`, `free acid`, `-`) is none: `N/A` isn't Na.

## Consequences

- A divalent metal's formula weight is never made from a ratio its compound
  can't have. Where Iki can't tell, it says so and asks, rather than guessing.
- A calcium salt of a diacid, or a magnesium salt of a weak N–H acid, needs
  its `Salt_eq` given to have a formula weight.
- An import without batches now fails a record whose Salt field names a salt
  Iki doesn't know, or contradicts its structure, as one with batches did:
  the field decides which names and IDs are the compound's, so it must be
  read. `on_error: skip` leaves such records out.
- Acid groups are found by substructure, not by pKa: a phenol or an imide
  isn't counted, so a divalent salt of one asks for its equivalents.

## Alternatives considered

- **Always require `Salt_eq` for a divalent metal.** Correct, but most such
  salts are of one carboxylic acid, where charge balance settles it; asking
  for what the structure already says is friction without safety.
- **Count every ionisable group, phenols and amides included.** That would
  call salicylic acid diprotic and refuse a ratio that is in fact settled;
  counting only the acids a metal salt forms at leaves the doubtful cases to
  the person who knows.
