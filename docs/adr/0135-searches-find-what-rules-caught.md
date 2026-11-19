# 0135. Searches find what rules caught, as fields every entity has

- Status: Accepted
- Date: 2026-11-18
- Refines: [0037](0037-entity-search.md),
  [0127](0127-plugins-rules-are-checked-at-every-save.md),
  [0122](0122-records-name-people-and-each-person-has-their-work.md)

## Context

Plugins' rules (ADR 0127) hold records for review, such as a result outside its
specification, and report records that break a rule added or changed after they were
saved. Each record's page says so, but QA's question is about many records: what's held
in this project, waiting for us? Which samples break the rule the last upgrade added?
LIMS answer it with an "on hold" queue, and GitHub with `is:` qualifiers beside
qualifiers that name a value (`is:locked`, `label:bug`).

Iki's searches are JSON conditions on fields (ADR 0037). Every entity has some built-in
ones (`hrid`, `made`, `assigned`, `upstream`), which the web app's search box also reads
as words (`assigned:me`, `needs:reassigning`, `downstream:DNR-000001`).

## Decision

**Two built-in fields, `held` and `breaks`,** of a kind of their own, `rule`:

- `held`: an open hold, by a rule that holds records for review.
- `breaks`: a breach, a rule the record breaks that it was saved before.

Each takes `is_set` (`true`: by any rule; `false`: by none), and a rule's name with
`eq`, `ne` and `in`, as its type lists it (an extension's is `plugin:name`):
`{"field": "held", "op": "eq", "value": "purity_in_specification"}`. With a type, a
name must be one of its rules that catches records that way, or one that holds or
breaks some of its records now (a rule a plugin dropped or changed still holds what it
held), so a typo is a 422 naming the rules there are. Without a type, any rule of that
name counts. They can't be sorted by. They're conditions like any other, so they
combine with the rest, are saved in saved searches and collections, and can be declared
in a component's search, checked by the validator against the type's rules.

They narrow what the searcher can already read; they don't widen it. A search's results
already carry each record's `holds` and `breaches`, so each result says which rule caught
it and why: the web grid shows a Rules column whenever a search asks what rules caught,
and the Python SDK's DataFrames have `held` and `breaks` columns naming the rules.

**The search box's words follow GitHub:** `is:held` and `is:breaking` find what any rule
caught, and `held:<rule>` and `breaks:<rule>` what one did. An `is:` qualifier, rather
than `held:any`, can't be mistaken for a rule named `any`.

**A field may not be named like a search's built-in fields.** A search names the built-in
field before the type's, so a plugin's field named `held` (or `assigned`, `made`,
`upstream`, `sent_to` and the rest) couldn't be searched. The validator refuses them,
as it refuses `hrid` and `type`.

## Consequences

- QA's queue is a saved search: `held` `is_set`, shared with the project.
- After a plugin's upgrade, Org Admins are told records break its rules (ADR 0127), and
  a search for `breaks` finds them, by rule.
- Plugins can't name fields `held`, `breaks`, `assigned`, `needs_reassigning`, `made`,
  `upstream`, `downstream` or `sent_to`. None in Iki's own did.

## Alternatives considered

- **A held-records endpoint.** A list of its own couldn't combine with the type's
  fields, a project, or a saved search, and would be one more endpoint to permission.
- **`held:any` and `breaks:any` words.** Shorter to explain, but `any` is a valid rule
  name.
- **Filtering on the client.** Pages are keyset pages over millions of records; held
  ones may be on none of the first.
