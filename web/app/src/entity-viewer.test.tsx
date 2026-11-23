/** Iki's own viewers on an entity's page: the sequence viewer, its features and its tracks. */

import { cleanup, screen, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer } from "./test-server";

const PLASMID = {
  ...PROTEIN,
  name: "iki/dna.DNASequence",
  plugin: "iki/dna",
  label: "DNA sequence",
  label_plural: "DNA sequences",
  hrid_prefix: "DNA",
  fields: [
    field({ name: "bases", label: "Bases", type: "string" }),
    field({ name: "features", label: "Features", type: "json" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      strands: null,
      topology: null,
      features: "features",
      tracks: [],
    },
  ],
};

const BASES = "ACGT".repeat(40); // 160 bp

function onPlasmid(features: unknown) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, PLASMID]);
  answer(
    "GET /api/v1/entities/DNA-000001",
    entity({
      hrid: "DNA-000001",
      type: "iki/dna.DNASequence",
      values: { name: "pTest", bases: BASES, features },
    }),
  );
}

describe("the sequence viewer", () => {
  it("is a tab with the sequence in rows, its features along it and in a table", async () => {
    onPlasmid([
      { type: "CDS", name: "tet", start: 5, end: 130, strand: "forward" },
      { type: "promoter", name: "P1", start: 150, end: 20, strand: "reverse" },
    ]);
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("160 bp · 2 features")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sequence" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("list", { name: "Feature kinds" })).toHaveTextContent(
      "CodingRegulatory",
    );
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual([
      "NameTypeLocationStrandLengthShow",
      "tetCDS5..130forward126Show",
      "P1promoter150..20 (across the origin)reverse31Show",
    ]);
    await user.click(screen.getByRole("button", { name: "Show tet" }));
    expect(screen.getByRole("button", { name: "Hide tet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("draws tracks of values under the residues, and says what they show", async () => {
    const LONG = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHV";
    const profile = Array.from({ length: LONG.length }, (_, i) =>
      i < 4 || i >= LONG.length - 4 ? null : (i % 5) - 2,
    );
    profile[20] = 2.5;
    profile[40] = -3.25;
    withTypes();
    answer("GET /api/v1/entity-types", [
      {
        ...PROTEIN,
        fields: [
          ...PROTEIN.fields,
          field({ name: "hydropathy", label: "Hydropathy", type: "json" }),
          field({ name: "charge", label: "Charge", type: "json" }),
          field({ name: "flexibility", label: "Flexibility", type: "json" }),
        ],
        viewers: [
          {
            viewer: "sequence" as const,
            label: "Sequence",
            sequence: "sequence",
            alphabet: "protein" as const,
            features: null,
            tracks: [
              { field: "hydropathy", label: "Hydropathy", min: -4.5, max: 4.5 },
              { field: "charge", label: "Charge", min: null, max: null },
              { field: "flexibility", label: "Flexibility", min: null, max: null },
            ],
          },
        ],
      },
    ]);
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({
        hrid: "PRT-000001",
        values: { name: "HBA", sequence: LONG, hydropathy: profile, charge: [1, 2] },
      }),
    );
    await renderApp("/entities/PRT-000001/viewers/sequence/");
    const legend = await screen.findByRole("list", { name: "Tracks" });
    expect(
      within(legend)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([
      "Hydropathy, on a scale from -4.5 to 4.5: highest 2.5 at 21, lowest -3.25 at 41",
      "Charge: not drawn, as it doesn’t have one number per residue",
      "Flexibility: none yet",
    ]);
    const bars = document.querySelectorAll("svg.value-track rect");
    expect(bars).toHaveLength(LONG.length - 8); // none for the first and last four
    expect(bars[16]?.textContent).toBe("Hydropathy at 21: 2.5");
    expect(bars[16]?.getAttribute("class")).toBe("track-above");
    expect(bars[36]?.getAttribute("class")).toBe("track-below");
  });

  it("counts a protein's residues without its final stop, and draws its track along them", async () => {
    // AAAAAGGGG*: nine residues, so ProtScale's window of nine gives one value, the fifth's.
    const profile = [null, null, null, null, 0.6, null, null, null, null];
    withTypes();
    answer("GET /api/v1/entity-types", [
      {
        ...PROTEIN,
        fields: [
          ...PROTEIN.fields,
          field({ name: "hydropathy", label: "Hydropathy", type: "json" }),
        ],
        viewers: [
          {
            viewer: "sequence" as const,
            label: "Sequence",
            sequence: "sequence",
            alphabet: "protein" as const,
            features: null,
            tracks: [{ field: "hydropathy", label: "Hydropathy", min: -4.5, max: 4.5 }],
          },
        ],
      },
    ]);
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({
        hrid: "PRT-000001",
        values: { name: "Stop", sequence: "AAAAAGGGG*", hydropathy: profile },
      }),
    );
    await renderApp("/entities/PRT-000001/viewers/sequence/");
    expect(await screen.findByText("9 residues")).toBeInTheDocument();
    const legend = screen.getByRole("list", { name: "Tracks" });
    expect(legend).toHaveTextContent("highest 0.6 at 5");
  });

  /** A protein type whose sequence viewer draws its hydropathy as a track. */
  function withHydropathy() {
    withTypes();
    answer("GET /api/v1/entity-types", [
      {
        ...PROTEIN,
        fields: [
          ...PROTEIN.fields,
          field({ name: "hydropathy", label: "Hydropathy", type: "json" }),
        ],
        viewers: [
          {
            viewer: "sequence" as const,
            label: "Sequence",
            sequence: "sequence",
            alphabet: "protein" as const,
            features: null,
            tracks: [{ field: "hydropathy", label: "Hydropathy", min: -4.5, max: 4.5 }],
          },
        ],
      },
    ]);
  }

  it("counts no stop inside a protein as a residue", async () => {
    // NCBI's standard code, keeping each stop as *, reads ATGAAATAAGTT as MK*V: 3 residues.
    withHydropathy();
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({ hrid: "PRT-000001", values: { name: "Inner", sequence: "MK*V" } }),
    );
    await renderApp("/entities/PRT-000001/viewers/sequence/");
    expect(await screen.findByText("3 residues")).toBeInTheDocument();
  });

  it("counts none of the stops ending a protein, and draws its track along the rest", async () => {
    // AAAAAGGGG then TAATAA, each stop kept: AAAAAGGGG**, nine residues, so ProtScale's
    // window of nine gives one value, the fifth's.
    withHydropathy();
    const profile = [null, null, null, null, 0.6, null, null, null, null];
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({
        hrid: "PRT-000001",
        values: { name: "Two stops", sequence: "AAAAAGGGG**", hydropathy: profile },
      }),
    );
    await renderApp("/entities/PRT-000001/viewers/sequence/");
    expect(await screen.findByText("9 residues")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Tracks" })).toHaveTextContent("highest 0.6 at 5");
  });

  it("shows an RNA as it's stored, U and all, its length in nucleotides", async () => {
    // A plugin's type: Iki's own have no RNA yet.
    const TRANSCRIPT = {
      ...PLASMID,
      name: "acme/rna.Transcript",
      plugin: "acme/rna",
      label: "Transcript",
      label_plural: "Transcripts",
      hrid_prefix: "RNA",
      viewers: [{ ...PLASMID.viewers[0], alphabet: "rna" as const, features: null }],
    };
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, TRANSCRIPT]);
    answer(
      "GET /api/v1/entities/RNA-000001",
      entity({
        hrid: "RNA-000001",
        type: "acme/rna.Transcript",
        values: { name: "mRNA", bases: "AUGGCCUAA" },
      }),
    );
    await renderApp("/entities/RNA-000001/viewers/sequence/");
    expect(await screen.findByText("9 nt")).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: /^Bases of/ })).toHaveTextContent("AUGGCCUAA");
  });

  it("counts a single strand of DNA in nucleotides, and a duplex in base pairs", async () => {
    // The field its viewer names says how many strands it has, as iki/dna's strandedness.
    const DNA = {
      ...PLASMID,
      fields: [
        ...PLASMID.fields,
        field({ name: "strandedness", label: "Strandedness", type: "string" }),
      ],
      viewers: [{ ...PLASMID.viewers[0], strands: "strandedness", features: null }],
    };
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, DNA]);
    for (const [hrid, strandedness] of [
      ["DNA-000001", "single"],
      ["DNA-000002", "double"],
    ] as const) {
      answer(
        `GET /api/v1/entities/${hrid}`,
        entity({
          hrid,
          type: "iki/dna.DNASequence",
          values: { name: "primer", bases: "ACGTACGTACGTACGTACGT", strandedness },
        }),
      );
    }
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("20 nt")).toBeInTheDocument();
    cleanup();
    await renderApp("/entities/DNA-000002/viewers/sequence/");
    expect(await screen.findByText("20 bp")).toBeInTheDocument();
  });

  it("leaves out features it can't draw", async () => {
    onPlasmid([{ type: "CDS", start: 0, end: 500 }, "junk"]);
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("160 bp · 0 features")).toBeInTheDocument();
    expect(screen.getByText("It has no features yet.")).toBeInTheDocument();
  });

  it("isn't a tab of types without it", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    await renderApp("/entities/PRT-000001/viewers/sequence/");
    expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
  });
});

/** A file of the repository's, from web/app (as pnpm runs the tests) or from its root. */
function repositoryFile(path: string): string {
  const here = join(process.cwd(), "..", "..", path);
  return readFileSync(existsSync(here) ? here : path, "utf8");
}

/** pUC19c's 2,686 bases (GenBank L09137.2), as the server's tests read them. */
const PUC19C = repositoryFile("server/tests/data/L09137.2.fasta").split("\n").slice(1).join("");

/** What copying says, beside the copy buttons. */
function copyStatus(): HTMLElement {
  const area = document.querySelector<HTMLElement>(".sequence-copy");
  if (!area) throw new Error("No copy buttons");
  return within(area).getByRole("status");
}

/** iki/dna's DNA sequence as the viewer sees it: its strands and topology named. */
const DUPLEX = {
  ...PLASMID,
  fields: [
    ...PLASMID.fields,
    field({ name: "strandedness", label: "Strandedness", type: "string" }),
    field({ name: "topology", label: "Topology", type: "string" }),
  ],
  viewers: [{ ...PLASMID.viewers[0], strands: "strandedness", topology: "topology" }],
};

/** pUC19c, a circular duplex, with its lacZα and its polylinker (L09137.2's features). */
function onPuc19(values: Record<string, unknown> = {}) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, DUPLEX]);
  answer(
    "GET /api/v1/entities/DNA-000001",
    entity({
      hrid: "DNA-000001",
      type: "iki/dna.DNASequence",
      values: {
        name: "pUC19c",
        bases: PUC19C,
        strandedness: "double",
        topology: "circular",
        features: [
          { type: "CDS", name: "lacZα", start: 146, end: 469, strand: "reverse" },
          { type: "misc_feature", name: "MCS", start: 397, end: 453 },
        ],
        ...values,
      },
    }),
  );
}

describe("the sequence viewer's strands and selection", () => {
  it("draws a duplex's other strand under it, 3′ to 5′, unless it's switched off", async () => {
    onPuc19();
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("2,686 bp · circular · 2 features")).toBeInTheDocument();
    // Each row's bases pair with those under them: pUC19c's TCGCGCGTTT from base 1, say,
    // with AGCGCGCAAA.
    const row = document.querySelector<HTMLElement>(".sequence-row");
    const start = Number(row?.dataset["index"]) * 60 + 1;
    const other = row?.querySelector(".sequence-complement");
    const pairs: Record<string, string> = { A: "T", C: "G", G: "C", T: "A" };
    const expected = PUC19C.slice(start - 1, start + 59).replace(/./g, (b) => pairs[b] ?? b);
    expect(other?.textContent).toBe(expected);
    expect(other).toHaveAttribute("aria-hidden", "true");
    const pairing = screen.getByRole("switch", { name: "Complementary strand, 3′ to 5′" });
    expect(pairing).toBeChecked();
    await user.click(pairing);
    expect(document.querySelector(".sequence-complement")).toBeNull();
  });

  it("draws no other strand for a single strand", async () => {
    onPuc19({ strandedness: "single" });
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("2,686 nt · circular · 2 features")).toBeInTheDocument();
    expect(document.querySelector(".sequence-complement")).toBeNull();
    expect(screen.queryByRole("switch", { name: /Complementary strand/ })).toBeNull();
  });

  it("selects by keyboard, as a text field does, and says what's selected", async () => {
    onPuc19();
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    const bases = await screen.findByRole("application", { name: "Bases of pUC19c" });
    expect(bases).toHaveAttribute("aria-roledescription", "sequence");
    const at = document.getElementById(bases.getAttribute("aria-describedby")?.split(" ")[1] ?? "");
    expect(at).toHaveAttribute("aria-live", "polite");
    expect(at).toHaveTextContent("Caret before base 1: T, paired with A.");
    const copy = screen.getByRole("button", { name: "Copy" });
    expect(copy).toBeDisabled();
    bases.focus();
    // pUC19c's first 24 bases: the plugin's gc_content gives 62.5, and its Tm 64.2 °C.
    for (let i = 0; i < 24; i++) await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expect(at).toHaveTextContent("Bases 1–24 selected: 24 bp · GC 62.5% · Tm 64.2 °C");
    await user.keyboard("{ArrowRight}");
    expect(at).toHaveTextContent("Caret before base 25: A, paired with T.");
    await user.keyboard("{Control>}a{/Control}");
    // The plugin's compute_composition records 50.63 for pUC19c.
    expect(at).toHaveTextContent(
      "Bases 1–2,686 selected: 2,686 bp · GC 50.63% · no Tm past 200 bases",
    );
    await user.keyboard("{Escape}");
    expect(at).toHaveTextContent("Caret after base 2,686, the last.");
    await user.keyboard("{Control>}{Home}{/Control}{ArrowDown}{End}");
    expect(at).toHaveTextContent("Caret before base 121");
  });

  it("copies the selection, or its reverse complement, and says so", async () => {
    onPuc19();
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    const bases = await screen.findByRole("application", { name: "Bases of pUC19c" });
    bases.focus();
    for (let i = 0; i < 6; i++) await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(await navigator.clipboard.readText()).toBe("TCGCGC");
    expect(copyStatus()).toHaveTextContent("Copied 6 bases.");
    await user.click(screen.getByRole("button", { name: "Copy reverse complement" }));
    expect(await navigator.clipboard.readText()).toBe("GCGCGA");
    expect(copyStatus()).toHaveTextContent("Copied the reverse complement of 6 bases.");
    bases.focus();
    await user.keyboard("{Shift>}{ArrowRight}{/Shift}{Control>}c{/Control}");
    expect(await navigator.clipboard.readText()).toBe("TCGCGCG");
    expect(copyStatus()).toHaveTextContent("Copied 7 bases.");
  });

  it("selects a feature from its bar, across the origin if it wraps it", async () => {
    onPuc19({
      features: [{ type: "promoter", name: "P", start: 2681, end: 4, strand: "forward" }],
    });
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    await screen.findByRole("application", { name: "Bases of pUC19c" });
    const [bar] = document.querySelectorAll("[data-feature]");
    expect(bar).toBeDefined();
    if (!bar) throw new Error("No feature bar");
    await user.pointer({ keys: "[MouseLeft]", target: bar });
    const at = document.querySelector(".sequence-at");
    // The last six of pUC19c and its first four, TTCGTCTCGC: 60% GC, and 29.1 °C in
    // sodium alone, which is what the readout beside a selection quotes (ADR 0201).
    expect(at).toHaveTextContent(
      "Bases 2,681–4, across the origin, selected: 10 bp · GC 60% · Tm 29.1 °C",
    );
    expect(screen.getByRole("application", { name: "Bases of pUC19c" })).toHaveFocus();
  });

  it("selects a protein's residues, with nothing to say of bases", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [
      {
        ...PROTEIN,
        viewers: [
          {
            viewer: "sequence" as const,
            label: "Sequence",
            sequence: "sequence",
            alphabet: "protein" as const,
            strands: null,
            topology: null,
            features: null,
            tracks: [],
          },
        ],
      },
    ]);
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({ hrid: "PRT-000001", values: { name: "HBA", sequence: "MVLSPADKTNVKAAW" } }),
    );
    const { user } = await renderApp("/entities/PRT-000001/viewers/sequence/");
    const residues = await screen.findByRole("application", { name: "Sequence of HBA" });
    residues.focus();
    await user.keyboard("{ArrowRight}{Shift>}{ArrowRight}{ArrowRight}{ArrowRight}{/Shift}");
    expect(document.querySelector(".sequence-at")).toHaveTextContent(
      "Residues 2–4 selected: 3 residues",
    );
    expect(screen.queryByRole("button", { name: "Copy reverse complement" })).toBeNull();
  });
});

describe("finding a motif in the sequence viewer", () => {
  it("finds bases on both strands, steps through them and selects each", async () => {
    onPuc19();
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    const search = await screen.findByRole("search", { name: "Find bases" });
    const box = within(search).getByRole("searchbox", { name: "Find" });
    const found = within(search).getByRole("status");
    // BstYI's RGATCY: seven sites in pUC19c, as Biopython's nt_search finds them.
    await user.type(box, "rgatcy");
    expect(found).toHaveTextContent("7 matches.");
    expect(document.querySelectorAll(".residue-hit").length).toBeGreaterThan(0);
    await user.keyboard("{Enter}");
    expect(found).toHaveTextContent("Match 1 of 7: bases 417–422, on both strands.");
    expect(document.querySelector(".sequence-at")).toHaveTextContent(
      "Bases 417–422 selected: 6 bp",
    );
    await user.click(within(search).getByRole("button", { name: "Previous" }));
    expect(found).toHaveTextContent(
      "Match 7 of 7: bases 2,341–2,346, on both strands, back at the end.",
    );
    await user.click(within(search).getByRole("button", { name: "Next" }));
    expect(found).toHaveTextContent(
      "Match 1 of 7: bases 417–422, on both strands, back at the start.",
    );
    // From the rows, N goes on to the next.
    screen.getByRole("application", { name: "Bases of pUC19c" }).focus();
    await user.keyboard("n");
    expect(found).toHaveTextContent("Match 2 of 7: bases 1,447–1,452");
    await user.keyboard("{Shift>}N{/Shift}");
    expect(found).toHaveTextContent("Match 1 of 7");
  });

  it("finds what reads across a circle's origin only when asked, and one strand if asked", async () => {
    onPuc19();
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    const search = await screen.findByRole("search", { name: "Find bases" });
    const found = within(search).getByRole("status");
    const origin = within(search).getByRole("checkbox", { name: "Across the origin" });
    expect(origin).toBeChecked(); // it's circular
    await user.type(within(search).getByRole("searchbox", { name: "Find" }), "GTCTCG");
    expect(found).toHaveTextContent("2 matches.");
    await user.keyboard("{Enter}{Enter}");
    expect(found).toHaveTextContent(
      "Match 2 of 2: bases 2,684–3, on the forward strand, across the origin.",
    );
    expect(document.querySelector(".sequence-at")).toHaveTextContent(
      "Bases 2,684–3, across the origin, selected: 6 bp",
    );
    await user.click(origin);
    expect(found).toHaveTextContent("1 match.");
    await user.click(within(search).getByRole("checkbox", { name: "Both strands" }));
    expect(found).toHaveTextContent("No matches.");
  });

  it("says what isn't a base", async () => {
    onPuc19();
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    const search = await screen.findByRole("search", { name: "Find bases" });
    const box = within(search).getByRole("searchbox", { name: "Find" });
    await user.type(box, "GAZJ");
    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(within(search).getByRole("status")).toHaveTextContent(
      "Z and J aren't IUPAC nucleotide codes.",
    );
    expect(within(search).getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("translations in the sequence viewer", () => {
  it("draws each coding sequence's translation under it, and says it at the caret", async () => {
    // The human insulin gene's coding sequence, as GenBank J00265.1 has it: the first
    // exon's start, from base 2424. Here at base 4, on a stretch of pUC19c given its bases.
    const bases = "TCGATGGCCCTGTGGATGCGCTAAGG";
    onPuc19({
      bases,
      topology: "linear",
      features: [
        {
          type: "CDS",
          name: "INS",
          start: 4,
          end: 24,
          strand: "forward",
          qualifiers: { codon_start: "1", translation: "MALWMR" },
        },
      ],
    });
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    const rows = await screen.findByRole("application", { name: "Bases of pUC19c" });
    const lane = document.querySelector(".translation-track .translation");
    expect(lane?.textContent).toBe("MALWMR*");
    // Each residue over its codon's three bases.
    const [first] = document.querySelectorAll<HTMLElement>(".translation-track .codon");
    expect(first?.style.left).toBe("3ch");
    expect(first?.style.width).toBe("3ch");
    rows.focus();
    for (let i = 0; i < 4; i++) await user.keyboard("{ArrowRight}");
    expect(document.querySelector(".sequence-at")).toHaveTextContent(
      "Caret before base 5: T, paired with A; in INS, codon 1, ATG: M.",
    );
    await user.click(screen.getByRole("switch", { name: "Coding sequences’ translations" }));
    expect(document.querySelector(".translation-track")).toBeNull();
  });

  it("reads any of the six frames, by the genetic code chosen", async () => {
    onPuc19({ bases: "ATGAAATGAGG", topology: "linear", features: [] });
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    await screen.findByRole("application", { name: "Bases of pUC19c" });
    const frames = screen.getByRole("group", { name: "Reading frames" });
    await user.click(within(frames).getByRole("checkbox", { name: "+1" }));
    await user.click(within(frames).getByRole("checkbox", { name: "−1" }));
    const lanes = document.querySelectorAll(".translation[data-label]");
    // ATG AAA TGA: M K *; the other strand, CCTCATTTCAT, reads CCT CAT TTC: P H F, drawn
    // along the bases as numbered, so right to left.
    expect([...lanes].map((l) => [l.getAttribute("data-label"), l.textContent])).toEqual([
      ["+1", "MK*"],
      ["−1", "FHP"],
    ]);
    // In the vertebrate mitochondrial code (2), TGA is tryptophan.
    await user.selectOptions(
      within(frames).getByRole("combobox"),
      within(frames).getByRole("option", { name: "2. Vertebrate Mitochondrial" }),
    );
    expect(document.querySelector(".translation[data-label]")?.textContent).toBe("MKW");
  });

  it("says which coding sequences it can't translate, and why", async () => {
    onPuc19({
      features: [{ type: "CDS", name: "odd", start: 1, end: 9, qualifiers: { transl_table: "7" } }],
    });
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(
      await screen.findByText("Not translated: odd (NCBI has no genetic code 7)."),
    ).toBeInTheDocument();
  });
});
