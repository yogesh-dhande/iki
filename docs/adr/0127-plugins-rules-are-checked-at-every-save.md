# 0127. Plugins' rules are checked at every save, and a rule can hold a record for review

- Status: Accepted (amended by [0297](0297-a-value-worked-out-from-a-records-own-values-is-a-computed-field.md): computed values are worked out at every save, before the rules; and by the 2026-11-27 amendment of [0082](0082-unique-fields-link-records-in-other-systems.md): unique fields are one-field rules, and `within: registry`)
- Date: 2026-11-18
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0056](0056-bulk-imports.md),
  [0082](0082-unique-fields-link-records-in-other-systems.md),
  [0090](0090-apps-are-compositions-of-ikis-components.md),
  [0096](0096-bundles-carry-entities-between-deployments-with-their-history.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md)

## Context

A manifest checks each field on its own: required, a range, a length. A lab's
data has rules across fields and records. One tube goes in each box position.
An instrument's bookings don't overlap. A sample's purity must meet its
specification. A booking needs an instrument that's in service. Without such
rules in Iki, every path that writes records (the web app, the API, MCP,
runs, bulk imports, bundles, migrations) needs its own checks, and the
database lets through whatever they miss.

Some of these rules aren't refusals. A result outside its specification
(OOS) is still data. The FDA's guidance on investigating OOS results says
it's recorded as measured and investigated, and a supervisor reviews the
investigation before the batch is dispositioned. Refusing the value invites
retesting until a result passes ("testing into compliance").

Rules can also change. A plugin's upgrade can add or tighten a rule that
records saved earlier break. Kubernetes's CRD validation ratcheting has a
precedent: an update that leaves a field alone isn't refused because of that
field's old value.

## Decision

**A type declares `rules`**, by name, each with a `label`, a `description`
and a `message` shown as text (never HTML), with CEL between `{{` and `}}`.
An extension can declare rules on the type it extends, as `plugin:name`.
There are three kinds:

- **`check`**: a CEL condition over `record` (the record as it will be saved,
  with each reference one step deep, as the referenced record's fields), and
  `now` and `today`. `when` limits it to some records, and `fields` says which
  fields its message points at. The default is the fields it reads.
- **`unique: [fields]`**: no two of a project's records share the values of
  a field or combination of fields, such as `[box, position]`. The database holds it.
- **`no_overlap: {from, to, per}`**: no two of a project's records' ranges
  overlap, with the same `per` values, such as an instrument's bookings. The
  database holds it with an exclusion constraint (`btree_gist`). Datetimes and
  numbers are half-open: one booking can start as another ends. Dates include
  both ends.

`unique` and `no_overlap` take `while: {field: [values]}` (enum, state or
bool fields) to count only some records, such as stored tubes or bookings
that aren't cancelled. Their fields are in the declaring manifest's own table,
not an identity's registry entry, except a `unique` rule `within: registry`, whose
fields are all its entries' (amended 2026-11-27, ADR 0082). A trigger keeps a row per
record (`iki_unique_value` under `table:rule`, or `iki_range_value`); a unique field
is a one-field rule kept the same way (ADR 0082). So every writer is held to the rule,
including SQL, and two saves at the same moment can't both get in. Before
writing, the server finds the record a save would clash with and names it.

**CEL, through iki-wasm.** The language is CEL, as apps' expressions are
(ADR 0090): side-effect free, deterministic and bounded. It runs in the
`cel` crate compiled to WebAssembly, which the server runs under wasmtime
with no imports and a fuel budget. Browsers run it in a Web Worker. Because
cel-rust has no type checker, iki-wasm has one. `iki plugin validate` type-checks
every rule against its type's fields: an unknown field (with a "did you mean"),
an enum value that isn't one of the field's values, text compared with a
number, and `matches` (no regular expressions) are refused there, with a line
and column, not at someone's save. The checker also records which fields each
rule reads.

Limits: 50 rules per type, expressions of at most 1,000 characters and 32
levels, messages of at most 500 characters, and a fuel budget per evaluation.
A save's checks are evaluated in one call, and a bulk import's in one call per
chunk.

**When it's checked.** Every write of a record's values is checked: a create
checks every rule, and an edit checks those reading a field it changes
(ratcheting). Rules apply to people, agents and service accounts alike. A
check that's false, or that can't be worked out for a reason other than an
empty value, breaks the rule. One that fails because a value it reads is empty
passes, as SQL's CHECK does with NULL, so an optional field isn't made
required by a rule about its value. A save that breaks rules is refused with
422 `validation_failed`, and each error names its `rule` and points at its
fields: `{"pointer": "#/values/purity", "detail": "…", "rule": "…", "hold": false}`.

**A rule can hold, not refuse** (`hold: {reviewed_by: [lab roles]}`), for
`check` rules only. A save that breaks only rules that hold is refused with
422 `hold_reason_required` unless it says why (`hold_reason`). With a reason,
the save is kept and the record is **held**: an open hold per rule, which
keeps the rule's label, message and roles as they were, who saved it, through
which agent, when, why, and the revision. Breaking the rule again while it's
held adds an observation to the same hold. Each observation keeps its own
message, actor, time, reason and revision, including its registry entry revision
when the record has one. A held record:

- keeps its hold if it's edited back within the rule, so it's not "tested into
  compliance": only a review clears a hold;
- can't be set to a state that locks it (ADR 0125), so a lot isn't approved or
  released while a result is being investigated;
- is reviewed by a person, not an app's or agent's token or a service account,
  who has one of the rule's lab roles in the record's project and didn't save
  any of the hold's observations. Review requires a reason, the current record
  ETag and the version of the hold evidence and deployed rules the reviewer read
  (`POST /entities/{ref}/holds/{id}/review`). A change requires a new review.

A run's results, and a bulk import's lines, that break rules that hold are
kept, held with a reason naming the run: they're what the instrument
measured. Holds and reviews are audited (`entity.held`,
`entity.hold_reviewed`) and sent to webhooks (identifiers and the rule, never
the message, which can quote values). The rule's reviewers are told in Iki.
A record's history keeps each observation with the revision that recorded it
(`holds` on its revisions). Once reviewed, it shows who reviewed the hold,
when and why. Earlier holds may have incomplete evidence of repeated failures.
Those gaps stay visible, and someone who contributed during that interval must
leave the review to someone else. A review changes no record values and creates
no revision.

**Records saved before a rule are reported, not stranded.** `sync_plugins`
checks every record again for each type whose checks, or whose plugins'
versions, changed since it last did (a digest per type). A record breaking a
rule that holds is held by the deploy. A record breaking any other rule gets
a breach, shown on its page and in the API (`breaches`), which the next save
that checks the rule and finds it kept clears. Each type whose records were
checked is audited (`plugin.rules_checked`) with counts, and Org Admins are
told. A bundle's
records are checked the same way when they're imported: they're kept as they
were made, and reported or held here. `unique` and `no_overlap` rules can't
be broken by records already there: the deploy stops, naming them, as it does
for unique fields.

**Forms check as you type.** A type's `rules`, and its `rule_variables` (the
schemas iki-wasm evaluates against), are in `GET /entity-types`, so a form
evaluates checks with the same module, inline before saving. It asks for a
reason when only rules that hold are broken.

## Consequences

- A lab's rules are declared once, in its plugin, and hold on every path.
  The database holds those it can.
- An out-of-specification result is recorded and visible, with who saved it,
  why, and who reviewed it, rather than lost to a refused save.
- A plugin's upgrade can tighten rules without failing saves of unrelated
  fields, and what it finds is reported at deploy.
- Rules read one step of references. A rule over many records, such as a
  count, isn't expressible. That's a later step's (graph queries), not CEL's.
- A rule checks what's saved. It isn't a permission: who may edit is still the
  project's roles, and who may set a state is its `set_by` and guards (ADR
  0128).
- CEL's timestamps and durations come through chrono and chrono-tz, which
  RustSec marks unmaintained (not vulnerable). They run only in iki-wasm,
  which has no imports. `worker/deny.toml` and `osv-scanner.toml` ignore those
  advisories with reasons. The OSV ignore expires on 2027-02-18, which forces
  a revisit. They go once cel moves to jiff or time.
- Rules' times are UTC, and the checker refuses time zones. So
  `worker/.cargo/config.toml` builds chrono-tz with only the UTC zone. That
  keeps about 0.9 MB of zone tables out of the module browsers load. The
  server image's build and a spec test fail on a module that has every zone.

## Alternatives considered

- **Python in plugins.** It can do anything, which is the problem: not
  sandboxed, not bounded, not checkable before a save, and not runnable in a
  browser.
- **JSON Schema `if/then`.** It can't compare two fields, or read a referenced
  record, and its errors don't say what a person did wrong.
- **Rego or JSONLogic.** Rego is for policy over documents, harder to read in a
  manifest, and has no browser runtime as small as iki-wasm's. JSONLogic has
  no types to check.
- **Refuse out-of-specification values.** It's simpler, but it loses data and
  invites retesting until a result passes, against the FDA's OOS guidance.
- **Check every rule on every edit.** A rule added later would block unrelated
  edits of every record that breaks it. Ratcheting, with a deploy's report,
  doesn't.
