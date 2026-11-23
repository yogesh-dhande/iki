# @ikisci/science

Scientific custom elements and functions for web applications. Import the subpath you
use. Each element takes JavaScript data and dispatches DOM events. All public entries
include TypeScript declarations. The booking calendar can run on a static file server
without an Iki project or account.

## Booking calendar

Import `@ikisci/science/booking-calendar` and create `<iki-booking-calendar>`.
Set `.month` to a local calendar month (`YYYY-MM`). Assign `.data` whenever the
bookings or their loading state change. Each booking supplies an opaque string ID,
title and ISO timestamp with time zone. It may also include an end timestamp and an
instrument label. The example is a fictitious reservation.

```js
import "@ikisci/science/booking-calendar";

const calendar = document.createElement("iki-booking-calendar");
calendar.month = "2026-03";
calendar.data = {
  label: "Instrument room",
  bookings: [
    {
      id: "demo-1",
      title: "Instrument check",
      instrument: "Plate reader",
      startsAt: "2026-03-09T10:00:00Z",
      endsAt: "2026-03-09T11:00:00Z",
    },
  ],
};
calendar.addEventListener("booking-open", (event) => console.log(event.detail.id));
document.body.append(calendar);
```

Use the interval in a `month-change` event to load a different month. The end
is exclusive. See the [data and event contract](stories/booking-calendar.md)
and [runnable story](stories/booking-calendar.html).

## Storage and scientific inputs

| Entry                                         | Plain data                                                                                                                                                            | DOM event                                                             |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| [`box-map`](stories/box-map.md)               | A label, rectangular shape with its numbering order, and an array of occupants with one-based row and column. Optional selections, marks and reconciliation findings. | `slot-activate` carries the row, column, position label and occupant. |
| [`quantity-input`](stories/quantity-input.md) | A label, unit choices, reference unit and `{ number, unit, other }` strings. A custom unit can be entered as a UCUM code.                                             | `value-change` carries the edited quantity draft.                     |
| [`sequence-input`](stories/sequence-input.md) | A label and string value. Use `format: "helm"` for multiline notation and supply a live parser description in `reading`.                                              | `value-change` carries the text as entered.                           |

The corresponding element tags are `<iki-box-map>`, `<iki-quantity-input>` and `<iki-sequence-input>`. Assign the data as a JavaScript property. Load `@ikisci/ui/tokens.css` and `@ikisci/science/box-map.css` to style the box map. The editors share `@ikisci/science/science-inputs.css`.

## Scientific functions

| Import                        | Data and result                                                                                                                                                                                                                                       |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `@ikisci/science/parsers`     | `await loadParsers()` returns readers for UTF-8 FASTA sequences or alignments, and HELM strings. [Inputs, coordinate conventions and results](stories/parsers.md)                                                                                     |
| `@ikisci/science/sequence`    | Plain DNA, RNA or protein strings, annotated features and per-residue numeric tracks. Functions for complement, GC content, exact motif matching, melting temperature, feature layout and track scales. [Inputs and coordinates](stories/sequence.md) |
| `@ikisci/science/translation` | Translate a whole frame or an annotated coding feature with one of the NCBI genetic codes. [Coordinates and qualifiers](stories/translation.md)                                                                                                       |

## Oligonucleotide and complex views

| Entry                                 | Element data and interaction                                                                                                                                                                                                                                                          |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `@ikisci/science/oligo-chemistry`     | `<iki-oligo-chemistry>` takes a `nucleotides` array ordered 5′ to 3′ and optional `ends`. Each position can describe a base, sugar, linker and modifications. [Contract](stories/oligo-chemistry.md) · [Story](stories/oligo-chemistry.html)                                          |
| `@ikisci/science/oligo-duplex`        | `<iki-oligo-duplex>` takes the `sense` and `antisense` strings, both 5′ to 3′, and `pairs` with 1-based antiparallel coordinates. [Contract](stories/oligo-duplex.md) · [Story](stories/oligo-duplex.html)                                                                            |
| `@ikisci/science/complex-composition` | `<iki-complex-composition>` takes named `parts`, optional residue-to-residue `bonds` and a `reduced` formula, masses and ε280. Activating its optional compute button emits `compute-request`. [Contract](stories/complex-composition.md) · [Story](stories/complex-composition.html) |

Assign these objects to the element's `.data` property. A user opening or closing the
positions, strands or bonds disclosure emits a bubbling `details-change` event with
`{ section, open }`. The `@ikisci/science/composition-mass` entry independently
exports `withDisulfides`, `absorbanceAtOneGramPerLitre` and `formulaAtoms`.
[Mass, extinction and absorbance units](stories/complex-composition.md#masses-and-absorbance)

## Molecular calculations

These subpaths run without a browser or a server.

| Entry under `@ikisci/science/`                            | Data and results                                                                                                                                                                   |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`molecular-properties`](stories/molecular-properties.md) | Neutral formula, peptide or DNA sequence to average and monoisotopic masses in u. Peptide composition and strand chemistry give ε280 or ε260 in L/(mol·cm).                        |
| [`open-reading-frames`](stories/open-reading-frames.md)   | An IUPAC nucleotide sequence, NCBI table and topology to annotated complete ORFs on both strands, including the translated residues.                                               |
| [`restriction-map`](stories/restriction-map.md)           | Molecule length, topology and existing cut positions to fragment lengths and coordinates. A recorded paired interval can project a primer's 3′-relative offset onto the reference. |

## Sequence and map viewers

| Public entry        | Element                 | Data and events                                                                                                                                                                                               |
| ------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sequence-viewer`   | `iki-sequence-viewer`   | Sequence and alphabet, recorded annotations, optional library results with provenance, and per-position numeric tracks. Emits selection and feature changes. [Contract and story](stories/sequence-viewer.md) |
| `plasmid-map`       | `iki-plasmid-map`       | Topology, length and independent feature, ORF and binding-site layers. Emits view, layer and primer actions. [Contract and story](stories/plasmid-map.md)                                                     |
| `plasmid-thumbnail` | `iki-plasmid-thumbnail` | Title, length, topology flag and annotations. An optional labeled button emits an open request. [Contract and story](stories/plasmid-thumbnail.md)                                                            |

## Alignment viewer

Import `@ikisci/science/alignment-viewer` and assign `.data` to
`<iki-alignment-viewer>`. Supply an alignment, its title and optional reference
row, row labels and initial position. The element emits selection, position and
navigation events. The separate `@ikisci/science/alignment-compare` entry compares
aligned rows as plain data. [Data and events](stories/alignment-viewer.md) ·
[Alignment story](stories/alignment-viewer.html)

## Next.js

Create the element inside a Client Component. Import the browser module in an
effect so custom-element registration runs after mount. Types can be imported
with `import type` on either side. [Next.js Client Components](https://nextjs.org/docs/app/getting-started/server-and-client-components)

```tsx
"use client";

import { useEffect, useRef } from "react";

export default function Calendar() {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    let active = true;
    void import("@ikisci/science/booking-calendar").then(() => {
      if (!node || !active) return;
      const calendar = document.createElement("iki-booking-calendar");
      calendar.data = { bookings: [] };
      node.replaceChildren(calendar);
    });
    return () => {
      active = false;
      node?.replaceChildren();
    };
  }, []);

  return <div ref={host} />;
}
```

## Run the story

From the repository root run `pnpm install --frozen-lockfile`, then
`pnpm --filter @ikisci/science build`. Serve `web/packages` with
`python3 -m http.server --directory web/packages 8000` and open
`/science/stories/booking-calendar.html`. The package build checks each public entry
against its gzip byte limit. The release workflow sets both package versions from the
Iki release tag.
