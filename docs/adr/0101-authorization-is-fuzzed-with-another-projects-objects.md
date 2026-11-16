# 0101. Authorization is fuzzed with another project's objects

- Status: Accepted
- Date: 2026-11-16

## Context

The authorization matrix (`test_api_access`) sends every operation's request
as every kind of person, and fails if an operation has no row. Each request,
though, names objects of one project. The commonest authorization bug, an
insecure direct object reference (IDOR), mixes them: a project's Admin sends
a path under their own project, or a body, naming an object in a project
they can't see. A check that confirms the project but not the object passes
the matrix. Workers have the same risk with jobs, and the live-update
streams are outside the OpenAPI document the matrix is checked against.

## Decision

**Every operation is attacked with another project's objects**
(`test_api_idor`):

- A second, foreign world is built beside the first, with people, teams and
  projects of its own. Each operation's request, built the matrix's way, is
  built in both.
- As the first world's Admin, each identifier the two requests differ in
  that belongs to the foreign project (found by following foreign keys to
  `Project`) is swapped in: in the path, the query or the body, one at a
  time and then all at once.
- Each such request must answer **exactly as it does when the identifier
  names nothing**, a key made up for the purpose: the same status and the
  same problem. It must never succeed, never answer 403 (which says the
  object exists), never fail on the server, and never return any of the
  foreign project's identifiers.
- The foreign project is then given one of everything every request makes,
  and everyone in the first world who isn't an organization admin or
  auditor calls every list and search, plain and filtered to it. Nothing of
  it may come back.
- The live-update streams refuse whoever reading the thing followed
  refuses, with the same status.

**Workers are attacked the same way** (`test_worker_idor`): two workers
each lease a job. Every job route, checked against the worker API's
document, is tried with the other's job, with the other's lease token, and
on one's own job with the other's token. Setups' routes have the same test.
The other job must be left as it was.

Each test was checked by breaking an access check on purpose (a webhook's,
the job lease's, the setup lease's): each then fails.

## Consequences

- An operation added later is attacked as soon as it has its matrix row,
  which every operation must.
- The setup stream answered 404 where reading the setup answered 403; it
  now refuses as reading does.
- The fuzzer swaps identifiers the requests already carry. A body field the
  matrix's request leaves out isn't tried: the matrix's requests should name
  what an operation can refer to.
