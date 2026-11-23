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

### One implementation of a measurement

The temperature reported for an unmodified oligo must agree in an action result, a
Python check and a browser selection. The fixed-condition calculation and rounding
live in the Rust source behind `iki-wasm`. The full module exposes it alongside the
file readers. The DNA action ships a small module compiled from the same source so
an isolated worker can measure a supplied sequence without a service call.

Git tracks the Rust source and the expected SHA-256 digest. The build compiles the
module with the pinned Rust toolchain. Before including it in either the downloadable
plugin archive or the worker image, the deployment CLI verifies its bytes against the
digest in the pinned plugin revision. A reviewer can rebuild that revision and compare
the result with the module extracted from the deployed artifact. [DNA plugin digest](../../plugins/dna/data/oligo-measurement.sha256)

For the initial module, independent builds on Linux x86-64 and macOS arm64 each
produced 11,925 bytes with SHA-256 `b33f7080ac30ed6de1a6acd51927709adc1ce486695b434ea953e0205ad83371`.

The selection viewer needs a synchronous result on the page. Its build translates
that small module to JavaScript, which keeps the page within its existing content
security policy. Build checks regenerate the artifacts, and tests compare their
answers with the Python reference, including undefined measurements. The
public restriction-map functions accept cut positions and paired intervals already
identified by the caller. Site discovery continues to run in the plasmid action.

## Consequences

A consumer can try each component with the data in its story and load just the entries
used on a page. Browser-only entries can be loaded after mount in a server-rendered
application. A change to an element's public contract affects ordinary npm consumers
as well as Iki's adapters. The release builds both packages from the same tag.
