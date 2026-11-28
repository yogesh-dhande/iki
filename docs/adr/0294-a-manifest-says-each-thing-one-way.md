# 0294. A manifest says each thing one way

- Status: Accepted
- Date: 2026-11-27
- Amends: [0032](0032-plugin-manifest-spec-v1.md) (the shape of the
  iki/v1 manifests, before they freeze at M1)
- Moves the shapes, not the behaviour, of:
  [0190](0190-receive-packs-into-storage-and-count-what-remains.md)
  (`stock_catalogue.notify`),
  [0243](0243-a-stability-protocol-plans-its-own-pulls.md) (`draw_target`)

## Context

ADR 0032 makes manifests data that people review and editors complete,
and at M1 their shape becomes a contract labs write against (ADR 0016).
They grew one block at a time, and the same idea came to be written
several ways. An author who learns one spelling meets another for the
same thing a file later:

- **Subtype and extension share a word.** An entity type's `extends:`
  makes a subtype (is-a); an extension's `extends_type:` adds fields to a
  type without making one.
- **"A state field in these states" has four shapes:** `{field, states}`
  (a field's `assignment`, a result's `released`), `{field, state}` (a
  request's `completed`), `{field, is}` (a draw target's `state`, where `is`
  was checked as an enum value and the others as names), and a rule's
  `while: {status: [stored]}`.
- **Lab-role lists have five keys:** `set_by` (a state), `by` (a draw
  target, each draw purpose, a request's `receives`), `reviewed_by` (a
  rule's hold), `notify` (a stock catalogue, a bare list) and `tell` (a
  notification, an object of fields, roles, creator, watchers and actor).
- **The action a trigger runs** is `run:`; a workflow step's is `action:`.
- **The schema-version constraint has two spellings and two grammars:** an
  action input's `schema: ">=1"` (no spaces allowed) and a dependency's
  `types: {T: ">= 1"}` (spaces allowed). On a json field, `schema:` already
  means a JSON Schema.
- **Some type references aren't typed:** `storage.holds` and
  `amount.aliquots.type` are plain strings, so editors can't complete them
  and nothing checks their pattern until the semantic stage.
- **An action's updates are keyed two ways:** its `effects.updates` is a
  list of `{input, fields}`, while its run output's `updates` is a map by
  input name. And a run output's create must name its type qualified
  (`acme/buffers.BufferPrep`), where every manifest takes `BufferPrep`.
- **Names under plugin.yaml's keys mix casings:** formats and writers are
  kebab-case (`mars-csv`, `tecan-worklist`); roles, APIs and settings are
  snake_case. Core's own formats are `nucleotide-fasta`, `protein-fasta`
  and `jcamp-dx`. These names are identifiers: in qualified references
  (`acme/lab.mars_csv`), in CEL, in paths.
- **An entity type is 30 keys side by side.** Four of them are one
  capability, a record's draws (`draw_guards`, `draw_purposes`,
  `draw_target`, `draws_received`), and three are its part in a service
  request (`request`, `result`, `work_item`), but nothing says they go
  together. Lab lists refuse each by name, so every new key has to be
  added to that refusal.

Precedent: Kubernetes' API conventions ask that one concept have one name
across all resources and that related fields be grouped in a sub-object
(`resources`, `securityContext`, a selector's one `{key, operator,
values}` shape); Swift and Kotlin keep `extension` apart from a subclass's
`:`. Iki's own records already name who did something `<verb>_by`
(`created_by`, `requested_by`, `decided_by`), and every other name under
plugin.yaml is snake_case.

## Decision

Pre-M1, with no compatibility kept: each old spelling is refused, and
every first-party plugin, example, the template and the docs use the new
one. What each key does, and how a subtype inherits it, doesn't change.

**An extension `adds_to:` a type.** `kind: EntityTypeExtension` names its
type with `adds_to:`. `extends:` stays on entity types, where it means
is-a, as in every language.

**"A state field in these states" is `{field, states}`, everywhere.** A
request's `completed`, a draw target's `state` and a rule's `while` take
it, as `assignment` and a result's `released` already did. `states` is a
list: a request completes in any of them, a draw target awaits its draw
in any of them, and a rule counts records in any of them. A rule's
`while` names one field, as no rule Iki's plugins declare needs two at
once. For an enum or a yes-or-no field, `states` holds its values, or
`true` or `false`. Triggers keep their `field` and `states` for now: their
selector is being rewritten on its own, and adopts this shape there.

**Who may act is `<verb>_by`; who is told is `notify`.** A list of lab
roles that may do something is named for what they do: `set_by` (a
state, and a review procedure's state, whose slots it names, in place
of `by`), `drawn_by` (a draw target, and each draw purpose, in place of
`by`), `received_by` (a request's `receives`, in place of `by`),
`reviewed_by` (a rule's hold). Who is told is always `notify:`, one
object: `fields`, `roles`, `creator`, `watchers`, `actor`. A
notification's `tell:` becomes `notify:`; a stock catalogue's
`notify: [roles]` becomes `notify: {roles: [...]}`, the same object,
restricted to what a stock alert can tell (roles in the item's project).
A key that names one role, `role:` on a signature, an approval or a person
field, stays.

**A trigger names its `action:`,** as a workflow step does.

**One schema-version constraint.** It's one definition, the one
plugin.yaml's dependencies already used (comparisons with whole versions,
comma-separated, spaces allowed), and both a dependency's `types:` and an
action input take it. An action input's key is `schema_version:`, as it
constrains the type's `schema_version`.

**Type references are `typeRef`.** `storage.holds` and
`amount.aliquots.type` have the pattern and editor completion every other
type reference has; their semantic checks stay.

**An action's updates are keyed by input, as its output's are.**
`effects.updates` is a map, `{<input>: {fields: [...]}}`. A run output's
create may name its type bare, resolved in the action's own plugin, as a
manifest does; a qualified name still names another plugin's type.

**Names under plugin.yaml's keys are snake_case.** Formats and writers
take snake_case names, as roles, APIs and settings do; core's formats are
`nucleotide_fasta`, `protein_fasta` and `jcamp_dx`.

**An entity type groups what goes together.** A record's draws are one
block, `draws: {guards, purposes, target, received}`, replacing
`draw_guards`, `draw_purposes`, `draw_target` and `draws_received`. A
type's part in a service request is one block, `service: {request,
result, work_item}`. Each part keeps its own inheritance: guards
accumulate down the lineage, purposes are inherited and may narrow,
`received` and `request` are inherited, and a subtype's `service.request`
may add only `service_fields`. A lab list refuses `draws` and `service` as
two keys.

## Consequences

- An author learns one spelling per idea, and an editor completes each
  type reference and each role list the same way.
- A hold reads as what it is: `reviewed_by: [qa]` is who reviews it, not
  who may set it.
- The stock alert's recipients can grow (fields, watchers) without a new
  shape.
- Scripts can write `BufferPrep` in a run output, as their manifest does.
- A format or writer's qualified name is a valid identifier wherever it's
  used.
- Grouped blocks show which keys go together, and a lab list's refusal
  doesn't grow with each draw or service key.
- Negative: every manifest changed at once. Pre-M1 that's a commit; after
  M1 it would have been a codemod per key.
- Negative: a provisional marker on `draw_purposes` (ADR 0293) now sits on
  `draws.purposes`.

## Alternatives considered

**For role lists:**

- **One literal key, `set_by`, for everyone who may act.** It reads right
  on a state, and wrong on a hold: "who may set the hold" when it's who
  reviews it. A hold is a GxP control, the one that keeps an
  out-of-specification result from being signed until QA reviews it, so a
  key that invites misreading it is worse than two keys.
- **`roles:` everywhere.** It drops the verb, and `notify.roles` already
  means who is told, so a bare `roles` can't say whether a list may act or
  is informed.
- **Keep `by:` where the block's name gives the verb.** `draw_target.by`
  and `receives.by` read only inside their block, and differ from
  `set_by` and `reviewed_by` beside them.
- **Keep `tell:` beside `notify:`.** Two words for one idea, and the stock
  catalogue's bare list couldn't grow into the notification's object.

**For grouping:**

- **Draws under `amount:`,** as first proposed. Records that receive a
  draw have no amount of their own (a sample, a stability pull, an
  experiment declare `target` and `received`), and a lot's guards sit on
  an abstract parent whose subtypes each add the amount; a type may not
  redeclare `amount`, so nesting would force those guards onto every
  subtype.
- **Leave the keys flat.** Nothing tells a reader the four draw keys are
  one capability, and the lab-list refusal grows with each key.
- **A block per service role (`request:`, `result:`, `work_item:` kept
  top level).** They're three roles in one relation (ADR 0132); one block
  says so, and keeps that relation's keys out of every other type's
  namespace.
