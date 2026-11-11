# 0049. MCP tools call Iki's REST API in-process, as the agent

- Status: Accepted
- Date: 2026-11-11

## Context

ADR 0013 puts an MCP server at `/mcp`, with OAuth 2.1, Iki's permission
service on every call, `via=mcp` audit and labels on untrusted content. It
leaves open how the tools reach Iki's data. They could call the services
directly, with their own checks and their own answers. But then every tool
would be a second way in. Its permission checks, validation and errors
would drift from the API's, and the authorization test matrix (ADR 0003)
would need a twin.

The MCP Python SDK serves Streamable HTTP as a Starlette app with a session
manager that must be running before it answers. Django, which answers
everything else, has no lifespan hook.

## Decision

- **Every tool is a thin layer over a REST endpoint.** It sends an
  in-process ASGI request to `/api/v1/...` with the agent's own bearer
  token. That request goes through all of Django's middleware, so the
  tool can do exactly what the endpoint allows the person the agent acts
  for: their roles, and the token's scopes and projects. It counts against
  their rate limit. Tools answer with the endpoint's documents and fail
  with its problems, which agents read as tool errors that list each
  invalid field.
- **The audit channel can't be forged.** The `/mcp` request and the API
  requests its tools make are recorded with `via=mcp` and the OAuth
  client's name. Tools mark their requests with a key in the ASGI scope,
  which no client can set, rather than with a header.
- **Stateless, JSON responses.** Each MCP request stands alone, so any pod
  can answer any request, and nothing is kept between them. The web
  process's ASGI lifespan starts the SDK's session manager, and uvicorn
  runs with the lifespan on, so the process doesn't start without it.
- **Django answers first.** It checks the Host header (`ALLOWED_HOSTS`),
  logs the request and checks the bearer token. The view refuses
  requests with an `Origin` other than Iki's own, which is MCP's guard
  against DNS rebinding. Without a valid token, it answers 401 with a
  challenge pointing at `/.well-known/oauth-protected-resource`
  (RFC 9728).
- **Tokens for the MCP server serve its tools.** MCP clients may ask for a
  token for `https://iki.example.com/mcp` (RFC 8707). Iki accepts such a
  token on `/mcp` and on the API calls its tools make, and nowhere else.
- **Data is fenced as data.** Every answer comes inside
  `<iki-data-…>` markers, with a random suffix that the data can't guess
  and so can't close early. A line before the block says the text was
  written by people and tools and isn't instructions. The server's
  instructions say the same. Answers also carry the JSON as structured
  content.
- **Tools say what they do.**
  - Read-only: `whoami`, `list_projects`, `list_entity_types`,
    `describe_entity_type`, `search_entities`, `get_entity` (with
    provenance), `get_entity_history`, `get_lineage`, `list_actions`,
    `describe_action`, `list_runs`, `get_run` and `get_run_log`.
  - Adding: `create_entity`.
  - Changing, marked destructive so clients ask first: `update_entity` and
    `set_entity_archived`. They name the revision they were decided on
    (`If-Match`), and need a reason for the entity's history.
  - Tools that start runs follow, with approvals for runs on the container
    and VM tiers (ADR 0013).
- **Revisions name the app.** Each revision records the OAuth app it came
  through, as the audit log names it. An agent's change reads as made by
  the person it acted for, through MCP, through that app.

## Consequences

- Positive: one implementation of every rule. An endpoint's permission
  checks, validation and tests cover its tool too, and a fix to one fixes
  both.
- Positive: an agent's work reads in the audit log and in revisions
  exactly as anyone's does, marked as made through MCP by its client.
- Positive: stateless requests scale and fail over like the API's.
- Negative: each tool call costs an extra in-process request, and counts
  against the rate limit like one. That's small next to the work behind
  it.
- Negative: stateless means no server-to-client requests, such as asking
  the person a question mid-tool. Approvals happen in the web app instead.
- Negative: the fence is a mitigation. A model can still be steered by text
  it reads. Scopes, project grants and approvals remain the real limit.

## Alternatives considered

- **Tools calling the services directly:** a second path to keep in step
  with the API. Rejected for the drift described above.
- **Stateful sessions with SSE:** they need sticky routing or a shared
  event store, for features the tools don't use yet.
- **A header marking MCP's API calls:** a client could send it too.
