# 0084. Plugin components run in sandboxed frames

- Status: Accepted
- Date: 2026-11-14
- Refines: [0029](0029-react-web-app.md),
  [0047](0047-workers-run-only-the-code-a-run-was-launched-with.md)
- Refined by: [0170](0170-a-component-can-offer-its-plugins-actions-on-its-entity.md),
  on offering a plugin's actions from its tab
- Extended by: [0286](0286-the-component-package-is-the-frame-protocols-one-source.md),
  the frame protocol's one source, versioned and published

## Context

Plugins add types, actions and workflows; they also need to add to Iki's
pages. Most of what labs look at, Iki's own components can show if a plugin
says how: a structure coloured by a field, a sequence's features as tracks, a
dose-response curve as a chart spec, a grid's cells formatted by an
expression. Some things they can't: a plate map where people pick wells, a
circular plasmid map, a Sanger trace, a gel's lanes, a structure editor, a
gating tool for flow cytometry. Those need a plugin's own code in the
browser.

Until now plugin code never ran with a person's session. Actions run on
workers, in isolated pods, with only what their run gives them. Code in
Iki's page would run as the person: it could read everything they can, change
it as them, and send it anywhere. A bug or a compromised dependency in one
plugin would be every plugin's and Iki's problem, and the audit trail would
say the person did what the code did. Grafana and Backstage load plugins
into their page and trust them. VS Code's webviews, Figma's plugins and
Retool's custom components run them in sandboxed frames and talk to them
through messages.

## Decision

**Three tiers.** A plugin configures Iki's own components where they do
the job, which is most of the time (step 41 and 42). It adds a component of
its own only for what they can't show. And some things are no plugin's: an
external system's page embedded in Iki (a link out, as ADR 0082 gives, is
the way), the browser talking to instruments or other systems directly
(actions do that, through the egress proxy), and approvals, signatures,
audit and access, which stay in core.

**A component is a Web Component in one module.** A plugin declares it
under `components/`: its name, the module (one JavaScript file, everything
bundled, up to 10 MiB), the custom element the module defines, the slots it
appears in (for now, `entity.tab`: a tab on the pages of a type's entities),
and the events it sends (for now, `select`: entities a person picked, of a
declared type). The checks refuse a module that isn't a UTF-8 file in the
plugin, or is a symbolic link, or too big, and slots or events naming types
that don't exist. The build copies each module into the plugin's app with
the digest it had when it was checked, and the server checks the digest
again before serving it. What a frame runs is what was reviewed and pinned,
as with actions' code (ADR 0047).

**It runs in a sandboxed frame.** The web app shows a component in an
`iframe` with `sandbox="allow-scripts"`, so its document has an origin of
its own that is no one's: it can't read Iki's cookies, storage or pages, or
call Iki's API as the person. The frame's own policy (CSP) allows its module
and Iki's bridge, by digest, and nothing else: no network (`connect-src
'none'`), no other scripts, no forms, no frames of its own. The sandbox
keeps it from navigating Iki's page, opening windows or downloading. The
policy also says `sandbox`, so the frame is sandboxed however it's opened,
and only Iki's pages may frame it. The frame and its module are addressed
by the module's digest, so browsers keep them for ever, and neither needs
or gets the person's session.

**Iki gives it data, and does what it asks.** Inside the frame, Iki's
bridge creates the element and sets its properties: the entity whose page it
is on, as the person may read it. The element tells the page what happened
with DOM events (`iki-select`, with the HRIDs of the entities picked), which
the bridge passes on through a `MessageChannel` port the page hands it once.
The page checks every message against what the component declared, and
does anything that matters itself: it fetches with the person's permissions,
and writes, runs and navigation go through Iki's own screens, with their
confirmations. A selection, say, is read back from the API, as the person
may read it, and offered to the run page as the grid's is.

## Consequences

- A component can only show what Iki gives it and tell the page what a
  person did. It can't read other data, change anything, or send data out
  by network requests. Browsers leave a few channels that no policy closes:
  a frame can open another page in itself, with data in its address, and
  some browsers allow WebRTC and DNS prefetching. The page notices a frame
  that opens another page, stops talking to it and takes it away, saying
  which plugin did it; but what was in the address has gone. So plugin
  code is still reviewed, pinned and signed into the image, as all plugin
  code is.
- Plugin authors write an ordinary custom element. Libraries that fetch
  their own data, or load code from elsewhere, must be bundled and given
  data instead. WebAssembly and workers work.
- Each component costs a frame, so one per tab or panel, never one per grid
  cell: cells are formatted by expressions (step 42).
- Components that need more from the page, such as files to read or
  values to save, get them as the bridge grows, each behind a declaration
  the checks can see.
