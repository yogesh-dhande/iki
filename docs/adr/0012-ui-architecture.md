# 0012. UI: React host, deploy-time Web Components, apps as trusted compositions

- Status: Accepted
- Date: 2026-11-09

## Context

Plugins need to add viewers and widgets, such as structure and sequence
viewers. Users and AI agents want to build custom dashboards ("apps") over
their data. Letting anyone, especially an agent, ship arbitrary JavaScript
into the browser would let it read data the viewer can see and send it
elsewhere. The UI must also update live as runs progress.

## Decision

- **Host app**: React, TypeScript and Vite, with TanStack Query and
  Router, a generated API client, and a design system with theming and dark
  mode. It targets WCAG 2.2 AA and is ready for translation, English
  first.
- **Plugin UI** ships as Web Components (standard custom HTML elements,
  independent of any framework), bundled into the image **at deploy time**.
  Nothing is loaded at runtime.
- Each component declares a props schema, the events it emits and the data
  it needs. It **fetches its own data with the viewer's permissions**.
  Components fill slots on entity, run and project pages.
- **Apps** (`core.App` entities) are validated composition specs: a layout,
  component instances and props, bindings between components, and a safe,
  pure, CEL-like expression language. **No untrusted code runs.** New
  primitives arrive through plugin PRs.
- App writes are limited to the app's project, or projects declared in its
  manifest and accepted by the viewer. The host asks for confirmation
  before egress actions, external links to allowlisted domains, downloads
  and clipboard writes.
- **Saved views** are `core.View` entities pinned to exact revisions.
- **Live updates** use one authenticated SSE (Server-Sent Events, a
  one-way HTTP stream from server to browser) connection per tab.
- A strict CSP applies to the whole app. Heavy parsing runs in Rust
  compiled to WebAssembly (`iki-wasm`) inside Web Workers.

## Consequences

- Positive: agents can build useful apps through MCP without any path to
  run code in the browser.
- Positive: views and apps are entities, so they get revisions, provenance
  and lineage.
- Positive: plugin authors are not tied to React.
- Negative: a new kind of widget needs a plugin change and a deploy.
- Negative: we must design, document and maintain an expression language
  and a composition spec.
- Negative: Web Components inside a React host need careful bridging for
  props and events.
