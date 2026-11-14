# 0085. Types configure Iki's own viewers

- Status: Accepted
- Date: 2026-11-14
- Refines: [0084](0084-plugin-components-run-in-sandboxed-frames.md)

## Context

ADR 0084 sets out three tiers for what plugins add to Iki's pages, and says
the first, configuring Iki's own components, does the job most of the time.
The commonest things labs look at in Iki are sequences and their
annotations, and structures. Every lab tool draws a sequence the same way:
residues in numbered rows, annotations as bars under them. A plugin that
had to ship its own component for that would cost each lab a frame, a
bundle to review, and a look of its own.

Benchling, SnapGene and Geneious each draw features from the record's own
annotations; GenBank's feature table is the shared vocabulary (a key, a
location, a strand, qualifiers). The dna plugin already keeps a sequence's
features in that shape.

## Decision

**A type names the fields Iki's viewers show.** Under `display.viewers`,
each viewer says which it is and which fields it shows. The first is
`sequence`: a string field with a sequence format, and optionally a json
field of features (`type`, `start`, `end`, `name`, `strand`). The validator
checks that each field exists and is the kind its viewer shows. Subtypes
show their nearest parent's viewers unless they declare their own, as with
the display title.

**Each viewer is a tab, drawn by the web app.** It's part of Iki, so it
runs in the page with Iki's own policy and reads what the page already has:
the entity, as the person may read it. The sequence viewer draws residues in
rows of 60 in groups of 10, numbered, and each feature as a bar under the
residues it covers, in a lane of its own where features overlap, coloured
by its kind (coding, regulatory, terminator, origin, binding site, other)
and pointing the way its strand runs. A feature that ends before it starts
wraps the origin of a circular sequence. Features are also listed in a
table, which is how a keyboard or screen reader reaches them; showing one
marks its residues. Rows off screen aren't drawn, so a genome opens as
quickly as a primer. A feature that can't be drawn, out of range or
malformed, is left out rather than hiding the rest.

**More viewers come the same way.** A structure viewer (Mol*), and tracks
such as per-residue values, are further entries under `display.viewers`,
each with the fields it shows.

## Consequences

- Plugins get a viewer by naming fields: no code, nothing to review beyond
  the manifest, and the same look across plugins.
- What a viewer can show is what Iki builds. A lab needing more writes a
  component (ADR 0084), or asks for the viewer to grow.
- The viewers are Iki's code, so they're tested and accessible as the rest
  of the app is, and follow its light and dark themes.
