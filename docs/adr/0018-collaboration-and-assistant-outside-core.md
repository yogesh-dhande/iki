# 0018. Collaboration and the in-app assistant stay out of core

- Status: Accepted (amended 2026-11-22: no assistant of Iki's own)
- Date: 2026-11-09

## Context

Users will want comments, tags, notes, watching entities, and an assistant
inside the app. These features are valuable but not essential to Iki's
core job: typed entities, actions, and provenance. Every feature in core
must meet the M1 quality bar in security, accessibility and tests, and
core must stay small enough to secure well. Iki already has a plugin system
that can add types, UI and actions (ADR 0006).

## Decision

- **Collaboration** (comments, tags, notes, watch) is not part of core. It's
  planned as a future plugin (`collab`).
- **Iki builds no assistant of its own,** in core or as a plugin. People
  bring the agent they already use, such as Claude, and connect it through
  the MCP server (ADR 0013). It acts with the same checks, approvals and
  attribution as the person it acts for (ADRs 0050, 0072). Iki's job is to
  make those tools and their answers good, not to compete with the agents.
- Core provides the hooks those plugins need:
  - **polymorphic refs** (a `ref` field to `core.Entity` can point at any
    entity type), so a comment can attach to anything;
  - **UI slots** on entity, run and project pages (ADR 0012);
  - a **notifications API** (in-app, email and signed webhooks) that
    plugins can call;
  - **triggers** (ADR 0011), so plugins can react to events.
- AI agents reach Iki through the MCP server (ADR 0013), which is in core.

## Consequences

- Positive: core stays focused and the M1 scope stays achievable at full
  quality.
- Positive: building these features as plugins proves that the extension
  hooks are good enough for real use.
- Positive: enterprises that do not want them, or want their own, can
  leave them out.
- Negative: M1 ships without comments or tags, and with no assistant inside
  the app; someone without an agent of their own has none.
- Negative: the hooks must be designed now, before their main consumers
  exist, so some may need revision later.
