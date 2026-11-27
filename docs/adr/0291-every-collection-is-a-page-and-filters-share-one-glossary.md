# 0291. Every collection is a page, and filters share one glossary

- Status: Accepted
- Date: 2026-11-27
- Amends: [0027](0027-rest-api-conventions-and-service-accounts.md)

## Context

ADR 0027 says lists use cursors: a page is `{"items": [...], "next_cursor":
...}` and `limit` is 1–100, 50 by default. The API grew past it. Of its 178
GET operations, 51 answered pages, 44 a bare JSON array (40 with no paging at
all, 4 with a `limit` and no cursor), and some lists were cut at a fixed
size (200 people, 500 places, 20 reconciliations, the first 50 lookups) with
nothing to say there were more. Two other answers reused `items` without
being pages (`/storage/scan`'s `{code, items}`, `/entities/{ref}/signatures`'
`{items, missing}`). The Python SDK called the page size `page_size` and
defaulted it to 100 in three places and 50 in a fourth.

Filters drifted the same way. `GET /entities` took `type` (one) and `types`
(several), while lineage walks and `/me/work` took `type` as an array.
`project` was a UUID everywhere but the lab lists, which also took a slug.
`/quarantine` took both `status` and `state`, for different things. Text
search was `q` in ten places and `search` in one. `workflow` was a
workflow's name on `/workflow-runs` and a workflow run's ID on `/runs`. A few
collections had singular paths (`/quarantine`, `/compute/slurm`).

Once M1 freezes the API, turning an array into a page, or renaming a
parameter, breaks every client. GitHub, Stripe (`{data, has_more}`), Slack
and Benchling (`nextToken`) page every list, short ones too, so a client
reads every list one way.

## Decision

**Every collection is a page.** Every GET that answers a collection answers
0027's page: `items`, and `next_cursor`, null on the last page. It takes
`limit` (1–100, 50 by default) and `cursor`, a signed value from the
previous page's `next_cursor`. That holds for short and bounded lists too:
the installed types, actions and workflows, the project and lab roles, a
dashboard's choices, a list's options. A list held in memory (what installed
plugins declare) is paged by a unique sort key, as a table is; a list built
from rows is paged in SQL, its filters in the query. No collection is cut
at a fixed size: where a list used to stop at 200 or 500, it pages.
A page may say more about the whole list beside them, as GitHub's search
says its `total_count`: a lineage walk's `root`, `depth` and `total`, or the
records a page of amount movements refers to (`refs`).

What isn't a collection:

- **one resource**, however many lists it holds: an entity, a run, a type;
- **a summary computed over collections**: counts, costs, the compute
  overview, and `/search`, which answers a few results of each kind and links
  each kind's own list (its `per_kind` says how many);
- **a set replaced whole**: a person's organization roles (`PUT`), the
  deployment's signing keys, which verifiers read whole, as a JWK Set is;
- **logs**, read by byte offset, which are text, not items;
- **a lookup's few best matches**: a scan's `matches` for a code, at most
  20, since a code that matches more names nothing.

**`items` is a page's, and nothing else's.** A list inside another answer is
named for what it holds: a scan's `matches`, a record's `signatures`, a
search group's `results`, a move's `moves`, an import's `outside` records'
`first`. A field's or parameter's `items` is JSON Schema's word for a list's
item type, a declaration, not a list.

**One filter glossary**, for every query parameter:

| Parameter        | Means                                                                                                                                                                                                                                                                                                                                         |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `limit`          | Items per page, 1–100, 50 by default. Never anything else.                                                                                                                                                                                                                                                                                    |
| `cursor`         | The previous page's `next_cursor`.                                                                                                                                                                                                                                                                                                            |
| `q`              | Text to look for. Never `search`, `text` or `query`.                                                                                                                                                                                                                                                                                          |
| `type`           | The items' entity types: repeat it for several. On lists of records each takes its subtypes, unless `exact_type`; on other lists it matches what the item says (a mapping's `type`, the request types a service takes). A parameter naming another thing's type says whose (`entity_type`, the type whose `field` names people on `/people`). |
| `project`        | A project's ID, a UUID; repeat it where several may be given. Slugs are for the web app's addresses, never the API's.                                                                                                                                                                                                                         |
| `state`          | A state: of a record's state fields, as `field.state`, or of a resource whose field is `state` (a setup build, a quarantine finding).                                                                                                                                                                                                         |
| `status`         | Only where the resource's own field is `status`: runs, workflow runs, webhook deliveries. No resource takes both.                                                                                                                                                                                                                             |
| booleans         | Named for the condition: `archived`, `released`, `superseded`, `unread`, `revoked`, `active`, `member`, `mine`.                                                                                                                                                                                                                               |
| a kind's name    | `action`, `workflow`, `kind`, `format`, `role`: by name. `workflow` is always a workflow's name.                                                                                                                                                                                                                                              |
| an instance's ID | `project`, `run`, `batch`, `workflow_run`, `sample`, `collection`, `custodian`: by ID, or HRID where it has one. `/runs` takes `workflow_run` for a workflow run's steps.                                                                                                                                                                     |
| `sort`           | The order, where a list offers more than one.                                                                                                                                                                                                                                                                                                 |

A filter takes the name of the field it filters, so the answer says the same
thing the question did: `/runs`' `workflow_run` filters each run's
`workflow_run`.

**Collection paths are plural nouns**, under a namespace where one helps
(`/storage/places`, `/compute/slurm-clusters`, `/projects/{id}/stock/items`).
A path that names how its items relate to the resource above stays as it is
(`upstream`, `downstream`, `sent-to`, `sent-from`, `checked-out`,
`awaiting-put-away`). The singular ones moved:

- `/quarantine` → `/quarantine-decisions` (what each item is, and what its
  `{decision_id}` names);
- `/compute/slurm` → `/compute/slurm-clusters`;
- `/projects/{id}/stock` → `/projects/{id}/stock/items`, beside
  `/stock/orders`, with each item's lots at `/stock/items/{item_id}/lots`;
- `/me/work` and `/me/work/open` → `/me/work-items` and
  `/me/open-work-items`;
- `/entities/{ref}/making` → `/entities/{ref}/makings`, one per tool, as
  search already counts them;
- `/receipts/{id}/history` and `/reconciliations/{id}/history` →
  `.../observations`.

**MCP and the SDKs say it the same way.** MCP tools are named verb_noun
(`whoami` stays, as the convention it is); a tool that lists takes `limit`
and `cursor` and answers the page. The Python SDK's page size is `limit`,
50 by default, everywhere, and iterating a `Results` follows `next_cursor`.
The web app reads pages; a screen that needs a whole collection, such as a
dropdown of a list's options, follows the cursors with one shared helper.

**A test keeps it so.** Every GET in the OpenAPI document whose answer is a
JSON array fails the build, and every collection GET must take `limit` and
`cursor` and answer `items` and `next_cursor`.

## Consequences

- Positive:
  - A client reads every list one way, and a list that grows past a page
    keeps working: nothing silently stops at 200.
  - A parameter means the same thing wherever it appears, so a script that
    filters runs by `project` and `status` filters workflow runs the same way.
  - The OpenAPI document, the SDKs and MCP all show the same shapes.
- Negative / trade-offs:
  - A screen that shows a short list whole, such as installed actions, now
    follows cursors: usually one request, sometimes two.
  - Lists held in memory are paged by their sort key, so an installed plugin
    changing between two pages (a deploy) can shift what the next page holds.
  - Breaking, for every client: pre-M1, there are none to keep.

## Alternatives considered

- **Pages only for lists that can grow**: a judgment per endpoint, made again
  for every new one, and a list that "can't grow" (a project's services, a
  type's apps) usually can.
- **`{data, has_more}`, as Stripe does**: as good, but 0027 already chose
  `items` and `next_cursor`, and 51 operations use them.
- **Keeping `types` beside `type`**: two parameters for one filter, and a
  rule for what both together mean.
