# 0286. The component package is the frame protocol's one source

- Status: Accepted
- Date: 2026-11-27
- Extends: [0084](0084-plugin-components-run-in-sandboxed-frames.md),
  [0117](0117-components-show-many-entities-in-more-places.md),
  [0170](0170-a-component-can-offer-its-plugins-actions-on-its-entity.md)

## Context

A plugin's component talks to Iki through a sandboxed frame (ADR 0084): Iki's
page hands Iki's bridge in the frame one port, sets `place`, `entity` and
`entities` on the element through it, and reads back what the element asks
for. That exchange is the frame protocol, and at M1 it becomes a public
contract, like the manifest and the run input.

It was written down three times, by hand. `@ikisci/component` held what
authors import; the web app's `components.ts` held the page's own message
types, refinement type and validators; and `bridge.js` held which events
become which messages. They had drifted:

- ADR 0170 added `iki-run` and `place.runs`. The host accepted `run`
  messages and the bridge forwarded `iki-run`, but the package had no `run()`,
  no `EVENTS.run` and no `Place.runs`. Thirteen first-party components
  dispatched `new CustomEvent("iki-run", …)` by hand.
- The package's `Entity` had no `refs`, which the host sends and which a
  revision link (`open(element, record, revision)`) depends on.
- The same limits had different names: `MAX_CONDITIONS` and `MAX_DEPTH` in
  the package, `MAX_REFINE_CONDITIONS` and `MAX_REFINE_DEPTH` in the host and
  the server. The package's operators lacked `of`, which the API takes.
- The package wasn't published (the release's npm matrix was `[science,
ui]`), and the only bundler that resolved it was a script with six
  first-party components hard-coded. An author outside the repository could
  not install what the README told them to import, or build with it.

There was also no version. A frame's address was its module's digest, and
browsers keep a frame for ever, with the bridge inlined in it. ADR 0170
noted that a later bridge change existing modules rely on would need the
bridge's digest in the address too.

Other platforms keep a frame protocol in one published package for the same
reasons: Figma's plugin typings and Slack's Block Kit types are what both the
host and authors build against, versioned with the host.

## Decision

**`@ikisci/component` is the protocol's one source.** It holds:

- what a component is given: `Place` (with `runs`), `Entity` (with `refs`
  and `people`), `ShownEntity`, `Entities`, `Refinement`, `Condition` and its
  `Operator`s;
- what it may ask for: `EVENTS` and the helpers that send them, `select`,
  `more`, `refine`, `open` and now `run`;
- the messages: `Connect`, `HostMessage` (page to bridge) and `FrameMessage`
  (bridge to page);
- the limits, by one set of names: `MAX_CONDITIONS`, `MAX_DEPTH`,
  `MAX_REFINEMENT_SIZE`, `MAX_QUERY_LENGTH`, `REFINABLE_COLUMNS`,
  `MAX_SELECTED`, the frame's heights, and the `HRID` and `RECORD_UUID`
  patterns;
- the validators Iki's page reads a frame's messages with, `readMessage` and
  `readRefinement`, in `@ikisci/component/host`. They take a `Declaration`
  (what `GET /components` lists), so the package stays free of Iki's SDK.

Iki's page imports all of it and keeps none of its own. The server's limits
have the package's names, and a server test holds them equal.

**The bridge is tested against the package.** A web test runs `bridge.js` as
the server serves it and checks that it forwards exactly `EVENTS`, that each
helper's event arrives as the message the page's `readMessage` accepts, and
that anything else an element dispatches never reaches the page.

**The protocol has a version.** `PROTOCOL` is 1. The page sends it in
`iki-connect`, and the bridge connects only for the version it speaks; the
bridge sends it in `ready`, and the page accepts only its own. A change to a
message is a new version that both sides name, so a later change can be
negotiated instead of misread. A frame's address now carries the bridge's
digest beside the module's, so a browser never runs a frame it kept with an
older bridge, and an out-of-date page asking for one gets a 404, not a
mismatched bridge.

**The package is published, with its bundler.** It's in the release's npm
matrix, at Iki's version, so an author uses the package of the Iki their
deployment runs. It carries `iki-component-bundle`, which builds each
`web/src/<name>.js` of a plugin, with everything it imports, into the
`components/<name>.js` its manifest's `module` names, and refuses a module
that would import anything at run time. `--check` fails if a built module
isn't what its source builds. It needs rolldown, an optional peer
dependency; the package has no other.

**Every component is built the same way.** Each first-party and example
component's source is in its plugin's `web/src/`, sends its events with the
package's helpers, and is built by the published bundler; the repository's
`pnpm bundle:plugins` runs it over every plugin (small_molecule's depiction,
which packs RDKit.js, keeps its own build). No component dispatches the
protocol's events by hand. The plugin template's CI checks a plugin's
components the same way, when its `web/` is a package, and the
`iki-write-plugin` skill teaches it.

## Consequences

- Adding a message or an event changes one package, and the page, the
  bridge's test and the authors' types follow from it. A drift like `run`'s
  fails a test instead of shipping.
- Plugin authors outside the repository can install the types, the helpers
  and the bundler at their Iki's version, and check their build in CI.
- Every first-party component module is now generated, so `components/*.js`
  is ignored by the linters and formatter and its source is linted instead.
  Their modules' digests changed, and with the bridge's digest in frames'
  addresses, every frame's address is new once.
- A component that sends an event the bridge doesn't forward is ignored, as
  before; one built against a newer protocol still runs on an older Iki,
  whose bridge simply never forwards what it doesn't know.
