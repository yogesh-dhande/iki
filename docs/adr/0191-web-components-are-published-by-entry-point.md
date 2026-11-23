# 0191. Web components are published by entry point

- Status: Accepted
- Date: 2026-11-22
- Extends [0084](0084-plugin-components-run-in-sandboxed-frames.md).

## Context

A developer making an instrument schedule should be able to display the calendar with
their own booking data. Today its input is an Iki query result, and changing month asks
the plugin host to refine that query. The docs site also needs the controls, typography
and themes used on the corresponding Iki pages.

## Decision

Ship two Apache-2.0 npm packages with each Iki release. Scientific components and
computations belong in `@ikisci/science`. Design tokens, Inter and shared interface
components belong in `@ikisci/ui`. Inter retains its upstream font licence.

Each component registers a custom element from its own ES module, accepts ordinary
data and reports user interaction with DOM events. An application owns its reads,
writes and navigation. Calculations and file readers have separate function entries.
Public subpaths have TypeScript declarations and a compressed byte budget which
includes their dependencies and assets. Importing a calendar or a map must work
independently of the structure viewer, depiction engine and their assets.

Iki adapters translate between the public contract and the app or plugin bridge.
A plugin ships its adapter and renderer in one checked-in, reproducible JavaScript
bundle. That file goes through the existing digest check and iframe policy. For
server-rendered HTML the UI package and Django serve the same styles and font files.

## Consequences

A consumer can try each component with the data in its story and load just the entries
used on a page. Browser-only entries can be loaded after mount in a server-rendered
application. A change to an element's public contract affects ordinary npm consumers
as well as Iki's adapters. The release builds both packages from the same tag.

A Firefox test clicked the sidebar’s New project link and remained on Home.
Investigating the failed test showed that an empty saved-search response could
redraw the plus icon between pointer-down and pointer-up. The link stayed in place,
but Firefox lost the click when its original SVG path was removed. Navigation
components therefore retain each continuing control and its descendants during
refreshes, updating labels, attributes and paths in place. The browser regression
holds the pointer on the path while the response arrives, then releases it at the
same coordinates.
