# 0290. Every problem code is declared, and described by its code

- Status: Accepted
- Date: 2026-11-27
- Amends: [0027](0027-rest-api-conventions-and-service-accounts.md)

## Context

ADR 0027 makes every error an RFC 9457 problem with a stable `code` for
programs. About 70 codes grew up across the server: the status defaults
(`not_found`, `validation_failed`, …), about 50 named where they're
raised, and a few chosen at run time (an export's `no_sequence`, a
component's `not_shown_here`). Some carry members a client acts on: a scan's
`candidates`, a guard's name, a starter's changed `plan`, a stale change's
lines.

Nothing listed them. The OpenAPI document said every operation's errors are
a `Problem` (0027's shape), but not which codes there are, which status each
comes with, or which members it carries, so the TypeScript SDK typed 14 of
those members by hand as optional on every problem, and the Python SDK not at
all. One code meant two things (`too_many` was 409 on a collection and 422 on
a lineage export); `invalid` and `not_allowed` were used once each where
`validation_failed` and `refused` already meant the same; a 503 with no code
was `error`.

Once M1 freezes the API, a code's name, its status and its members are a
contract. Stripe's API returns a typed error object whose `code` a client
switches on; GitHub's OpenAPI description lists each operation's error
responses.

## Decision

**One catalogue declares every code**
(`server/src/iki_server/api/problem_codes.py`): its status, what it means,
the members it carries beyond every problem's, and which of them it always
carries. A code has one status and one meaning. The status defaults are in
it, with `unavailable` for a 503 that names none.

**A problem is one the catalogue declares.** Building a problem whose code
isn't declared, whose status isn't the code's, which carries a member the
code doesn't declare or lacks one it always carries, fails the test that
raised it (the test settings set `IKI_STRICT_PROBLEMS`). In production it's
answered as it is and logged as `problem.undeclared`: a lab's request
shouldn't fail because a message was misfiled. A test also reads the
server's source for every code it gives (a `Problem`'s, a refusal's `code`
attribute, a literal code an export or component exception carries) and
fails on one the catalogue doesn't declare, or on a declared code nothing
gives.

**The OpenAPI document describes each problem by its code.**

- A schema for each code that carries members (`ScanAmbiguousProblem`,
  `StaleProblem`, …), and one per status for the codes of it that carry none
  (`Plain404Problem`). Each holds every member a problem of it has, its
  `code` and `status` as enums, so a generated client needs no
  intersections.
- `Problem` is their union, discriminated by `code`; `Problem409` and its
  siblings are each status's.
- Each operation declares the errors its shape implies, each with its
  status's union: 401 and 403 when it's signed in, 404 when its path names
  something, 409 when it changes something, 422 when it takes input, and 412
  and 428 when it takes `If-Match` (ADR 0289), with the 412's `ETag`.
  `default` is any problem.

**The SDKs read problems from the document.** The TypeScript SDK's
`Problem` is the generated union, with `ClientProblem` for what the client
describes itself (`unexpected_response`, `network_error`).
`isProblem(value, code)` and `isApiError(error, code)` narrow to that code's
problem, whose members are then typed; `isStale` and
`isPreconditionRequired` are those for `stale` and `precondition_required`.
The Python SDK's `ApiError.problem` is the generated union, which a type
checker narrows by comparing `problem["code"]`.

**Codes changed** to keep one meaning each: a collection whose search finds
too many is 422 `too_many`, as a lineage export's is; a trigger's missing
service account or reason is `validation_failed`, naming the field; a
trigger that can't be turned on is `refused`.

## Consequences

- Positive:
  - A client knows every code, its status and its members from the
    document, and a generated client checks its handling of them.
  - A new code can't ship undeclared: the test that first raises it fails.
- Negative / trade-offs:
  - Declaring a code is one more step when writing a refusal.
  - The per-operation statuses come from the operation's shape, not from
    what its handler raises, so an operation may list a status it never
    answers; `default` still covers one it answers but doesn't list.
  - The OpenAPI document grows by about a third.

## Alternatives considered

- **Each operation lists the codes its handler raises**: precise, but
  nothing could keep it true short of tracing every call each handler
  makes, and a list that drifts is worse than one that's broad.
- **`type` as a URI per code, as RFC 9457 suggests**: 0027 chose
  `about:blank` and a `code`; a URI adds a second name for each code.
- **One schema with every member optional**, as the TypeScript SDK had:
  a client can't tell which members it may rely on.
