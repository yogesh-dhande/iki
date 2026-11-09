# 0013. Agents through MCP with OAuth 2.1 and human approvals

- Status: Accepted
- Date: 2026-11-09

## Context

Scientists want AI agents such as Claude Code and Codex to search data, run
actions and build views and apps. An agent acts with a user's authority but
without the user watching every step. It may also be steered by text inside
the data it reads (prompt injection). A runaway agent on the VM tier could
spend a lot of money quickly.

## Decision

- Iki exposes an **MCP server** at `/mcp`. MCP (Model Context Protocol) is
  an open protocol that lets AI agents discover and call tools. Iki uses
  the official Python SDK with the Streamable HTTP transport, mounted in
  the ASGI app.
- **OAuth 2.1 from day one**, via django-oauth-toolkit: PKCE (a per-login
  secret that stops a stolen authorization code from being used), dynamic
  client registration for MCP clients, a consent screen, scopes (`read`,
  `write`, `run`, `apps`), and project allowlists per grant.
- MCP calls go through the same permission service as every other path
  (ADR 0003).
- Agents may read and run cheap actions on their own. A run on the
  container or VM tier, or above the user's cost threshold, creates a
  pending `Approval`. The user approves it in the UI or through a
  notification link.
- User-authored text in tool responses is labelled as untrusted content.
- Every call is audited with `via=mcp` and the client name.
- Tools cover entities, runs, batches, workflows, lineage, the queue, views
  and apps, including `preview_app`, which returns a screenshot and
  errors.

## Consequences

- Positive: agents get useful access with scoped, revocable, per-project
  grants that SSO deactivation also revokes.
- Positive: the expensive and risky actions always have a human in the
  loop.
- Positive: audit records show exactly what an agent did.
- Negative: approvals add friction to expensive agent work.
- Negative: we track a young protocol and its authorization spec, and must
  follow changes to both.
- Negative: untrusted-content labels reduce prompt-injection risk but do
  not remove it. Scopes and approvals remain the real limit.
