# 0257. An antibody's methods are separate choices

- Status: Accepted
- Date: 2026-11-26

## Context

The antibody plugin did three kinds of work in one install: numbering chains
with ANARCI, screening them for sequence liabilities, and predicting structures
with ImmuneBuilder. Each is a method a lab chooses on its own.

- Numbering a chain that had been screened screened it again, on the new
  numbering, with the plugin's one rule set. A lab that renumbered a chain got
  a fresh liability assessment it hadn't asked for, and nothing on the chain
  said which rules had found its liabilities.
- The closest species and germline genes are assignments against the germlines
  one ANARCI release carries. The chain didn't say which, so a later release's
  different call couldn't be told apart, and `species` read like the antibody's
  origin.
- The structure provider brought a 2.7 GB image, half of it model weights, into
  every antibody install, with fields on every protein structure that only its
  predictions fill.

## Decision

Numbering, the liability screen and structure prediction are separate choices.

- `number_chains` doesn't apply a screen. When it numbers a screened chain in
  another scheme or with another CDR definition, it clears the screen, whose
  numbers and regions were the old numbering's, and says so. Numbered again the
  same way, the numbering is the same, so the screen stays. The chain's history
  keeps the cleared screen.
- `find_liabilities` takes a `rule_set` param. It ships one,
  `developability_v1`: LAP's motifs and severities, with deamidation and
  isomerisation ranked as Lu et al. measured them. The chain records the rule set
  and the numbering scheme and CDR definition the screen read in
  `liability_screen`. A new or changed rule set is a new name. The fields say
  that each hit is a potential site, predicted from sequence; a measurement is
  another record.
- Each numbered chain records its `germline_set`, the ANARCI release whose IMGT
  germlines assigned its species and genes. `species` is described as the
  closest germline species, not the sample's species.
- ImmuneBuilder moves to a first-party provider plugin, `iki/antibody_3d`, with
  its action, environment, image, weights, notice and checksums. Its extension
  of `iki/protein.ProteinStructure` holds `predicted_error` and
  `cdr_h3_predicted_error`, beside the model (`immunebuilder_model`) and release
  (`immunebuilder_version`) whose ensemble they measure. The image keeps its
  published name, `iki-antibody-structure-tools`, so its pinned digests stay
  valid.

The plugin is named for what it adds, not the tool: `iki/immunebuilder` would
make its extension's history table longer than PostgreSQL allows. Its action
names the tool, as every predictor's does.

## Consequences

A lab that registers, numbers and screens antibodies installs `iki/antibody`
and pulls no model image or weights. One that predicts structures adds
`iki/antibody_3d`. Another predictor would be another action, with its own
environment and its own confidence fields, since its errors aren't on
ImmuneBuilder's scale.

A chain's liabilities are always its current numbering's, and say which rules
found them. A lab that renumbers a chain screens it again deliberately.

The Antibody type's format and isotype vocabulary stay as they are; making them
a profile is identity work for its own change.
