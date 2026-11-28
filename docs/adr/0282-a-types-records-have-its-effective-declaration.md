# 0282. A type's records have its effective declaration

- Status: Accepted
- Date: 2026-11-27
- Makes explicit: [0007](0007-typed-table-per-type-entity-storage.md),
  [0032](0032-plugin-manifest-spec-v1.md),
  [0164](0164-a-subtype-says-which-roles-its-rows-take.md),
  [0165](0165-a-subtype-of-a-registered-type-has-registry-fields-of-its-own.md),
  [0232](0232-a-service-orders-its-own-work-from-primitives.md),
  [0235](0235-a-services-own-request-types-carry-its-fields.md)

## Context

A type declares only what it adds to its parent's: `extends` gives it the
rest. What "the rest" is was decided block by block, and each rule lived
where one consumer happened to need it. Storage, exports, viewers, the
display title, the due field, the amount, the result and the stock catalogue
were the nearest ancestor's; rules, notifications and draw guards added up;
draw purposes and `refines` could only narrow; a request's service fields
merged down; `sample` was any ancestor's. The rules were stated in comments
on Python dataclasses, and for `result`, `work_item`, `draw_target`,
`draws_received` and `stock_catalogue` nowhere at all. A plugin author
reading the manifest schema couldn't tell which rule a key followed.

The server, the validator, preflight and codegen each walked the chain
themselves: five independent walks, three `is_a` functions (two of them the
same code in two files), and four more on the client, which disagreed on
where to stop (`iki/core.Entity`, a missing type, a depth of 16) and on
guarding against a cycle. Two consumers of the same block could disagree:
the server's viewers were the nearest ancestor's, while the feature placement
for a registered sequence (ADR 0138) added up every ancestor's.

Schema languages that let one type refine another settle this once, in the
specification. CSS gives every property a definition table that says
whether it's inherited, so no browser decides it per property. LinkML, the
schema language many biology and clinical data models are written in,
refines an inherited slot in `slot_usage` and computes each class's
_induced_ slots, the resolved form tools read instead of walking `is_a`.
JSON Schema bundlers and OpenAPI tooling resolve `allOf` the same way.
ADR 0032 asks that "what a reviewer reads is what Iki reads": with each
consumer resolving inheritance its own way, that held only for a type
without a parent.

## Decision

**Every key of an `EntityType` manifest has one inheritance policy**, from
six, published in the entity-type schema's description of the key and in
`iki_spec.effective.POLICIES`, which a test holds equal:

| Policy       | Meaning                                                                                                                         | Keys                                                                                                                                               |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `own`        | Not inherited: each type's own.                                                                                                 | `name`, `label`, `label_plural`, `description`, `schema_version`, `extends`, `abstract`, `hrid`, `list`                                            |
| `nearest`    | A subtype has its nearest ancestor's, unless it declares its own, which replaces it.                                            | `display` (its `title` and `viewers` each), `exports`, `storage`, `due`, `result`, `work_item`, `draw_target`, `draws_received`, `stock_catalogue` |
| `once`       | Declared by one type of a lineage; its subtypes have it and can't declare another.                                              | `identity`, `amount`, `review` (a subtype that declares its own is refused, `review-inherited`)                                                    |
| `accumulate` | A subtype has every ancestor's and its own; none of its own is named as an ancestor's is.                                       | `fields`, `rules`, `notifications`, `draw_guards`, `sample` (once a sample type, its subtypes' records are samples)                                |
| `narrow`     | Inherited, and a subtype may only narrow it: its records take what its parent's do or less, so each is one of its parent's too. | `refines` (ADR 0283), `draw_purposes`                                                                                                              |
| `merge`      | A subtype has its nearest ancestor's, with what each type below adds.                                                           | `request`: a subtype adds service fields of its own (ADR 0232)                                                                                     |

These are the rules the blocks already followed where they were stated; the
one disagreement (feature placement's viewers) now follows `nearest`, as the
page does. A record type's review procedure, `review`, is `once`: its
subtypes inherit it whole, so every record of a lineage is reviewed the same
way, and the validator refuses a subtype declaring another
(`review-inherited`).

**Iki resolves each type's effective declaration once.**
`iki_spec.effective.resolve_one(types, name)` gives an `Effective`: the
type's lineage (itself, then its ancestors nearest first, ending with
`iki/core.Entity`), every field its records have, ancestors' first and each
as the nearest refinement narrows it, and every other block under its
policy. `sources` says whose declaration each inherited part is: a block by
its key (`storage`, `display.title`) and a named part by `key.name`
(`fields.material`, `rules.expiry_after_made`), with `refines.<field>` the
nearest type narrowing it. `is_a(wanted)` is `wanted in lineage`.

**The build publishes it.** `iki plugin build` and `iki deploy build` write
each plugin's types' effective declarations into its `manifest.json`, under
`effective`, keyed by qualified type name, beside the plugin's own
declaration. Empty blocks are left out. A reviewer reading a lab's subtype
sees exactly which storage, rules and narrowed fields its records get, and
from whom.

**Iki reads only that.** The server's `registry.effective(name)` reads it
from the manifest, and every server consumer reads its blocks there: shapes,
forms, checks, storage, amounts, requests, notifications, rules, exports,
viewers, components. Preflight, codegen and the validator resolve with the
same function over the types they have. The per-block walkers, the duplicate
lineage helpers and the `is_a` copies are gone; `registry.is_a` and
`effective.is_a` are the two entry points, over one lineage.

**The API says it.** A type's `TypeOut` gives `ancestors`, nearest first,
and the web app has one `isA(type, name)`, which plugin tabs, grids, box
contents and collection pages use.

**Extensions aren't part of it.** Another plugin's fields on a type are that
plugin's, added where it's installed; a type's manifest describes the type as
its plugin and its dependencies declare it.

**Collections stay a type.** "Is a collection" remains `is_a
iki/core.Collection`: a collection's behaviour is its fields (static members
or a search), so subclassing it is a real is-a (ADR 0038).

## Consequences

- A plugin author reads one table for what `extends` means, and the schema
  says each key's policy where the key is documented.
- A new inheritable block needs a policy before it can be added: the test
  over the schema fails for a key without one.
- `manifest.json` is larger: each type's fields appear once as declared and
  once as its records have them.
- The server can't disagree with the validator about what a subtype
  inherits: both read one resolver's output.
