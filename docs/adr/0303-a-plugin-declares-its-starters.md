# 0303. A plugin declares its starters

- Status: Accepted
- Date: 2026-11-27
- Supersedes in part: [0198](0198-a-project-applies-a-versioned-assay-starter.md) (the
  server's catalogue of recipes, and "example setup runs trusted server code")
- Refines: [0223](0223-applying-an-assay-starter-configures-the-project.md),
  [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md),
  [0247](0247-a-project-chooses-how-its-experiments-are-signed.md),
  [0266](0266-a-project-chooses-its-omics-results-review.md)

## Context

A starter is how a project's Admin chooses a type's review procedure and gives the
roles it needs (ADRs 0198, 0223, 0240). The nine recipes were Python in the server.
Each named its types, roles, flows, states, required roles and independent roles by
hand. A plugin could only say `starters: {dose-response: "1.0.0"}`, and Iki listed a
recipe only if the server knew it. So a lab's plugin couldn't offer a starter, equipment
had a flow setting and no way for a project to choose it, and a recipe's independent
roles could disagree with the guards that enforce them.

ADR 0198 kept recipes in the server because example setup ran trusted server code. ADR
0223 removed that: applying a starter creates no records. Its other reason, that a new
policy is a reviewed plugin change, holds better when the starter is in the plugin.

## Decision

**A starter is a manifest.** `kind: Starter`, in a plugin's `starters/` directory:

```yaml
apiVersion: iki/v1
kind: Starter
name: search_review
version: 1.0.0
label: Proteomics search results
description: Choose how newly imported search results are reviewed, and who signs them.
governs: ProteomicsResult
procedure: iki/core.result_review@1
flows: [research, regulated_qc, record_only]
suggested: research
slots:
  analyst: { minimum: editor }
  reviewer: { minimum: viewer }
  qa: { minimum: viewer }
```

- `governs` is the type whose records are pinned: one that's `pinned: create` or
  `opens: true` (ADR 0302). Records pinned `by` it follow, and the procedure must fit
  each of them too.
- `procedure` and `flows` are the procedure and the flows the project may choose;
  `suggested` is preselected (ADR 0302). A flow's `summary` is the procedure's unless
  `summaries` gives one for this work.
- `slots` gives each slot the flows use a minimum project role and, optionally, a
  `role` other than the default (ADR 0304).

It's referred to by qualified name, `iki/proteomics.search_review`, and its
semver `version` is what preview and apply name, as before. Any plugin can declare one,
for its own types or another plugin's it depends on, so a lab can offer its SOP's
procedure for Iki's types.

**What the recipe used to restate is derived.** The roles each flow requires are the
roles bound to the slots its states name. Who must be different people comes from the
flow's `independent_of` and `maker` declarations (ADR 0301): two slots must be
different people when one's state excludes the other's signers. Setup checks that the
chosen people can fill every slot with those pairs distinct, as it checked Hall's
condition before. The states each flow passes through are its route. None of these is
written in a starter.

**The setup API stays.** `GET /api/v1/starters` lists installed starters with their
governed types, procedure, flows (each with its states, required roles, the slots that
must be different people, and summary), slots and the project's current choice.
Preview and apply take the same body and plan as before. Applying a starter for a type
that another starter governs in the project replaces that choice, and the preview says
so.

**The validator checks starters** against the procedure and every type they pin: the
pairing is compatible (ADR 0301), each slot the flows use has a minimum role, and
`suggested` is one of `flows`. The deployment build checks that no two starters share a
name. Iki no longer has a catalogue of its own. Equipment gets a starter for its service
appointments.

Seeding example records isn't part of a starter. The tutorials load their own inputs
(ADR 0223).

## Consequences

- A lab's plugin can offer a starter, and a new procedure is a reviewed plugin change,
  as ADR 0198 wanted.
- A starter can't disagree with the procedure it applies: required roles, independent
  roles and states are read from it.
- Starter IDs change from `dose-response` to qualified names, and the plugin manifest's
  `starters:` key is removed. Before M1 nothing depends on the old names.
