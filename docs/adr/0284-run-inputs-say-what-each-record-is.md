# 0284. Run inputs say what each record is

- Status: Accepted
- Date: 2026-11-27
- Extends: [0042](0042-action-runs-and-jobs.md),
  [0105](0105-an-input-can-include-what-its-references-name.md),
  [0165](0165-a-subtype-of-a-registered-type-has-registry-fields-of-its-own.md),
  [0282](0282-a-types-records-have-its-effective-declaration.md)

## Context

An action's input takes its type's subtypes (ADR 0042), so a lab's
`GmpProteinLot`, extending `iki/lots.ProteinLot`, passes launch for an action
that takes protein lots. The record then arrives at the command with its
`type` and nothing else about its type. biologics_ms's characterization
compared `lot["type"] != "iki/lots.ProteinLot"` and refused the lab's lot with
"Select a protein lot of the chosen antibody". Scripts that need a record's
residues guessed the field (`("sequence", "bases", "helm")`), because
`iki/core.Sequence` declares none and each subtype names its own. Only a
lot's material was said, as `material_field`, and only for that one concept.

ADR 0165's amendment of 2026-11-27 gave each record its `types`, which this
decision keeps as it is; what follows adds the roles and `reads`.

Every input also carried every field of every record: a search over 100,000
compounds was sent every Molfile, 240 MB it never read. That's more than the
command needs to see.

Object systems answer "is this one of those" with the class hierarchy
(`isinstance`, `instanceof`); schema languages that hand data to code carry
the lineage with it. LinkML's `designates_type` and JSON-LD's `@type` give a
record its classes; FHIR resources give their `meta.profile` list. Least
privilege for a job's input, as GA4GH's Task Execution Service and workflow
languages declare inputs by what a step reads, is to send what it uses.

## Decision

**Every entity a run reads carries `types`**: its type, then each it extends,
nearest first, ending with `iki/core.Entity`, from its type's effective
declaration (ADR 0282). `type` stays its concrete type. A command asks "is it
one of these" with `wanted in entity["types"]`, and never compares `type`.
Lineage doesn't change while a plugin is installed, so every attempt of a
run is given the same.

**A type says which field plays each role Iki knows**, under `roles`:
`residues` (a string field with a sequence format, which is their alphabet)
and `formula` (its molecular formula). Its subtypes have its roles and can't
name another field for one (`role-rebound`), so whatever reads a DNA
sequence's residues reads a plasmid's. `roles` follows the `accumulate`
policy. What a record is of stays its amount's `material` (ADR 0139).

**Each entity a run reads carries `roles`**, the field playing each of its
type's: `material` (`id` or a field, replacing `material_field`),
`residues` (`field` and `alphabet`: `dna`, `rna`, `protein` or `helm`) and
`formula`. The API's description of a type gives the same, so a component or
an agent reads them as a command does. First-party scripts read a lot's
material, a chain's residues and a part's formula by role: characterization
accepts a lab's own protein lot of the antibody; composition weighs a lab's
own sequence type by its residues.

**An input can say what it `reads`**: fields of its type, its own or a
parent's (`reads-unknown-field`). Each of its entities then carries those,
the fields its roles name and the references it includes, and no others.
References and files in fields it doesn't read aren't sent, and needn't be
readable by the run's project. Without `reads`, an input carries every field,
as before.

## Consequences

- A lab's subtype of a first-party type works with first-party actions that
  take its parent, without the action knowing the lab's type.
- `material_field` is gone; scripts read `roles.material`.
- A concrete type extending `iki/core.Sequence` must name its residues field
  (ADR 0285).
- Run input documents are larger by each entity's lineage, and smaller by
  whatever `reads` leaves out.
