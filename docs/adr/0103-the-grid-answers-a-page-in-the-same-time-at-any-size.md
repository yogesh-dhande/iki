# 0103. The grid answers a page in the same time at any size

- Status: Accepted
- Date: 2026-11-16
- Refines: [0005](0005-postgres-only-data-infrastructure.md)

## Context

The grid shows a type's entities a page at a time (at most 100 rows), by
cursor, and never counts them (it says "100+"). A type's entities are a
join: the type's table, its parents' (a protein's is Sequence's, as a
DNA sequence's is: they're siblings), Entity's, the projects it's in, and
what its caller may read.

A load test filled a project with 100M proteins and opened the grid's
pages as a Viewer. At 1M rows, three kinds of page already took time in
proportion to the table, so they would have taken minutes at 100M:

- **A page deep into the table.** Postgres joined the type's tables with
  merge joins. The cursor's bound applied to the type's own table, but
  Postgres doesn't carry a bound on one table's key over to the tables
  joined by that key. So each parent's index was read from its start:
  0.9M rows of each, for a page 90% of the way in.
- **Sorted descending by a field.** Values come first and blanks last, and
  a plain index read backwards gives blanks first. So no index served the
  order, and every row was sorted.
- **A text search (`q`).** The trigram index found the one match in
  milliseconds. But the query joins more tables than Postgres reorders by
  default (8), so it joined them as written, matches last, and read every
  row first.

Searching for an HRID (`q`, such as `PRT-033333333`) slowed as the table
grew: 91 ms at 1M, 138 ms at 10M, 837 ms at 100M. Its trigram index reads
every HRID that has each of its trigrams, and an HRID's digits' trigrams
are in nearly every one.

Sorting by when entities last changed sorted every row too. Its index
leads with the type, which the query didn't name. Ties were also a
problem: a bulk import writes its rows at once, so millions share one
time, and their order didn't match the index's.

## Decision

**Searches plan for a page, in their own transaction.** Every search
already runs in one with a time limit. In it:

- merge joins are off, so each row's other tables are found by key, a
  page's worth wherever the page is;
- Postgres reorders up to 12 joins (`join_collapse_limit`,
  `from_collapse_limit`), which covers a type's tables, its projects and
  its caller's roles.

**Sorted pages read values, then blanks.** Entities with a value come
first, in an order the field's index reads. Descending, that's backwards,
with ties newest first: descending reverses ascending exactly, ties too.
Entities without a value come next, oldest first. A cursor in the blanks
goes on among them.

**Text shaped like the start of an HRID finds the HRIDs it starts.** A
prefix, a hyphen and some digits (`prt-0333`, in any case) is matched by
the HRIDs' own index, which reads only those. Other text still finds the
HRIDs it's in, and titles' text either way.

**Sorting by an entity's own columns names the type** (and its subtypes),
so the index on the type and the time serves it.

**A sort no index serves is refused at once when it's too long for a
page.** Before running one, the search asks Postgres for its estimate.
If the sort would take more than 1M rows (about half a second), the
search answers straight away. It doesn't wait out its 15 seconds. The
answer names the field:

> Sorting about 100 million proteins by Temperature optimum would take
> too long: it isn't indexed. Narrow the search with conditions on
> indexed fields, or sort by an indexed field.

Narrowed by an indexed condition, the same sort is fine, and runs.

**A load test proves it** (`test_scale_grid`: `make check-scale-grid`). A
project gets 100M proteins:

- one is made through Iki and copied in bulk, as a restore loads a
  database: each copy is a registry entry of its own, with the identity
  hash Iki would give it, and the project's record of it (ADR 0115);
- its indexes and unique constraints are built afterwards.

A small version, of 1,000 proteins, runs in every check, so the copying
keeps up with the schema before the nightly run needs it.

A Viewer then opens each kind of page, and each page has a budget of one
second (the median of five tries):

- the first page, the next, and one 90% of the way in;
- newest first;
- sorted by ID, last changed, and an indexed field, each with its next
  page;
- filtered by an indexed field and by name;
- searched by HRID;
- sorted by an unindexed field, which is refused at once.

The database is then 119 GB, more than CI's runners have, so CI runs the
test nightly at 10M rows (`.github/workflows/scale.yml`), and 100M runs
before a release, on a machine with the disk.

## Consequences

- A page takes about the same time at 1M, 10M and 100M rows. At 100M,
  locally (median of five, milliseconds):

  | Page                                  | 100M rows |
  | ------------------------------------- | --------- |
  | First, next, and 90% of the way in    | 30–33     |
  | Newest first, and its next page       | 31–32     |
  | By ID, either way, and the next pages | 61–70     |
  | Last changed first, and its next page | 54–86     |
  | Heaviest and lightest, and next pages | 52–66     |
  | A LIMS ID; a narrow weight range      | 22; 83    |
  | Names containing some text            | 84        |
  | An unindexed sort, refused            | 33        |

  Searching for an HRID by its start takes 55 ms at 10M rows, where the
  trigram search took 138 ms.

- Text like `RT-0333` finds HRIDs that start with it, not PRT-033333333:
  people type an HRID from its start.
- Descending sorts order ties newest first.
- An unindexed field can't sort a large type's grid, only a narrowed one.
  A plugin indexes the fields people sort by (`indexed: true`).
- A field's index is on the field alone. Ties among equal values are
  sorted within each value, so a field with millions of rows sharing one
  value sorts those millions. The time Iki keeps (when an entity changed)
  is indexed with the ID, so it doesn't.
- The tests' Postgres has 1GB of shared memory (`/dev/shm`), not Docker's
  64MB: a parallel search over millions of rows needs more. A
  deployment's database (RDS, or CloudNativePG's in development) has its
  own.
