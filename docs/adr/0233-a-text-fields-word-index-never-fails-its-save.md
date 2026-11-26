# 0233. A text field's word index never fails its save

- Status: Accepted
- Date: 2026-11-25
- Refines: [0037](0037-entity-search.md), [0033](0033-plugin-codegen-and-migrations.md)

## Context

Every text field has a full-text GIN index over its words
([ADR 0037](0037-entity-search.md)), and Postgres builds the index's
`tsvector` from the value whenever a record is saved. A `tsvector` holds at
most 1 MiB of distinct words and their positions, and positions up to 16,383
([PostgreSQL's limits](https://www.postgresql.org/docs/current/textsearch-limitations.html)).
For a bigger one Postgres raises `string is too long for tsvector`, and the
whole save fails. A text value can be far longer than this. Values that hit
the limit include:

- a Molfile of a large structure
- a long sequence in a text field
- a pasted instrument report
- protocol notes

A report of 200,000 different words, 1.8 MB of text, needs a 2.4 MB
`tsvector`, and fails. A value can't be refused for the sake of an index the
scientist never asked for.

Some text is machine text: a Molfile, a BigSMILES string or an instrument's
export. Its "words" are fragments of notation, which no one searches for.
Its full-text index costs space and writes, and it is the text most likely
to go over the limit.

Postgres users deal with this in two ways. They leave fields that don't need
word search out of the index. They index a bounded prefix of the field,
`to_tsvector(left(value, n))`, with the same expression in the index and the
query, so the query uses the index.

## Decision

**A text field can turn word search off.** A field takes `searchable: false`
([field schema](../../spec/src/iki_spec/schemas/field/v1.json)). It then gets
no full-text index. A search finds text in it with `contains`, and `matches`
is refused with the operators it does take. The server's search, a
component's declared search and a notification's conditions all check
`matches` against the same operator table. The API's `FieldOut.searchable`
says which text fields can be searched for words, so the web app's filters
offer "has the words" only for those. Text is searchable unless it says
otherwise, and only text fields take the option.

First-party machine text uses it: Polymer's `bigsmiles`. When the
small_molecule plugin stores a compound's Molfile, its `molfile` field uses
it too.

**A searchable field's words are its first 100,000 characters'.** Its index
is `to_tsvector('simple', left(value, 100000))`, and `matches` searches the
same expression (`conditions.WORDS_SEARCHED`), so the index serves it.
Measured against Postgres 18, a character adds at most 8 bytes to a
`tsvector`. The worst case is a hyphenated word of 4-byte letters, which is
kept both whole and as its parts. 100,000 characters therefore stay within
1 MiB however they are written, and a save never fails for the index. That
is some 15,000 words of prose. Postgres keeps word positions only up to
16,383, so phrase search over a longer value is already approximate. The
whole value is still stored, and `contains` searches all of it. The field's
schema description, the API's field description and the search
documentation all say that words are searched in the first 100,000
characters.

The fixed character prefix is chosen over an exact fallback that catches
the error and retries shorter. An exception handler in an index expression
makes a subtransaction on every write of every text field, and its result
depends on more than the value's length. A prefix is one expression, the
same in Django's index and in its query, and needs no function of Iki's own
in the database.

## Consequences

Any text value the field allows saves. A long note's later words aren't
found by `matches`, only by `contains`. Every text field's index is rebuilt
once by a migration in its plugin: core 0006, and one named
`bounded_word_search` in each first-party plugin with text fields. Marking
a field `searchable: false` drops its index in its plugin's next migration.
A saved search that uses `matches` on such a field then fails validation,
with a message saying which operators the field takes.
