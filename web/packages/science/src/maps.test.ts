import { afterEach, assert, describe, expect, it } from "vitest";
import "./plasmid-map.js";
import "./plasmid-thumbnail.js";

afterEach(() => {
  document.body.replaceChildren();
});

describe("plasmid map", () => {
  it("uses one-based closed spans, keeps the selected view and reports layer changes", () => {
    const map = document.createElement("iki-plasmid-map");
    map.data = {
      length: 400,
      name: "Synthetic circle",
      topology: "circular",
      parts: { items: [{ start: 350, end: 30, name: "Wrap", type: "misc_feature" }] },
      orfs: { items: [] },
    };
    document.body.append(map);
    const root = map.shadowRoot;
    assert(root);
    let view: unknown, layer: unknown;
    map.addEventListener("view-change", (event) => {
      view = (event as CustomEvent<unknown>).detail;
    });
    map.addEventListener("layer-change", (event) => {
      layer = (event as CustomEvent<unknown>).detail;
    });
    const button = (name: string) =>
      Array.from(root.querySelectorAll("button")).find((b) => b.textContent === name);
    expect(root.querySelector("table")?.textContent).toContain("350–400, 1–30");
    button("Linear")?.click();
    expect(view).toEqual({ view: "line" });
    map.data = { ...map.data, linkedPrimerIds: [] };
    expect(button("Linear")?.getAttribute("aria-pressed")).toBe("true");
    button("Library parts (1)")?.click();
    expect(layer).toEqual({ key: "parts", shown: false });
    expect(root.querySelector("table")).toBeNull();
  });

  it("offers only linked primer IDs to the host", () => {
    const map = document.createElement("iki-plasmid-map");
    const sites = ["first", "second"].map((primer) => ({
      primer,
      name: primer,
      start: 1,
      end: 2,
      three_prime: 2,
      annealed: 2,
      tail: 0,
    }));
    map.data = {
      name: "Synthetic segment",
      topology: "linear",
      length: 4,
      sites: { items: sites },
      linkedPrimerIds: ["first"],
    };
    let selected: unknown;
    document.body.addEventListener(
      "primer-open",
      (e) => {
        selected = (e as CustomEvent<unknown>).detail;
      },
      { once: true },
    );
    document.body.append(map);
    const buttons = map.shadowRoot?.querySelectorAll<HTMLButtonElement>("table button");
    expect(buttons).toHaveLength(1);
    buttons?.[0]?.click();
    expect(selected).toEqual({ id: "first" });
  });
});

describe("plasmid map with sequencing evidence", () => {
  // Synthetic: a 100-base circle, two bins of depth and two differences, one across the
  // origin, to check where each is drawn and said.
  const evidence = {
    name: "Synthetic circle",
    length: 100,
    topology: "circular" as const,
    parts: { items: [{ start: 90, end: 20, name: "Wrap", type: "misc_feature" }], own: true },
    coverage: {
      bin_size: 50,
      mean: [30, 4],
      min: [25, 2],
      minDepth: 10,
      meanDepth: 17,
      leastDepth: 2,
      low: [{ start: 60, end: 70 }],
      source: "Synthetic reads",
    },
    differences: {
      items: [
        {
          start: 99,
          end: 2,
          hgvs: "o.99_2del",
          kind: "deletion" as const,
          reference: "ACGT",
          observed: "",
          length_change: -4,
          reference_length: 4,
          observed_length: 0,
          homopolymer: 1,
          features: ["Wrap (misc_feature)"],
          depth: 30,
          support: 24,
          forward: 20,
          reverse: 4,
          fraction: 0.8,
        },
        {
          start: 40,
          end: 40,
          hgvs: "o.40A>G",
          kind: "substitution" as const,
          reference: "A",
          observed: "G",
          length_change: 0,
          reference_length: 1,
          observed_length: 1,
          homopolymer: 6,
        },
      ],
      source: "Synthetic aligner",
    },
  };

  it("draws the depth and differences, says them in words and tables, and toggles each", () => {
    const map = document.createElement("iki-plasmid-map");
    map.data = evidence;
    document.body.append(map);
    const root = map.shadowRoot;
    assert(root);
    expect(root.querySelectorAll("path.depth")).toHaveLength(1);
    expect(root.querySelectorAll("circle.threshold")).toHaveLength(1);
    expect(root.querySelectorAll("path.low")).toHaveLength(1);
    const said = root.querySelector("p")?.textContent ?? "";
    expect(said).toContain("17 reads deep on average, 2 at the least");
    expect(said).toContain("under the minimum depth of 10 at 60\u201370");
    expect(said).toContain(
      "2 differences from the record: o.40A>G at 40; o.99_2del at 99 to 2 in Wrap",
    );
    const captions = Array.from(root.querySelectorAll("caption"), (c) => c.textContent);
    expect(captions).toEqual(["Its features", "Read depth", "Differences from the record"]);
    expect(said).toContain("with 1 feature of its own: Wrap, 90 to 20");
    expect(root.querySelector('[aria-label="Layers"]')?.textContent).toBe(
      "Features (1)Read depthDifferences (2)",
    );
    const rows = Array.from(root.querySelectorAll('[aria-label="Differences"] tbody tr'), (row) =>
      Array.from(row.children, (cell) => cell.textContent),
    );
    expect(rows).toEqual([
      ["o.40A>G", "40", "substitution: A \u2192 G", "\u2014", "\u2014", "6 bases"],
      [
        "o.99_2del",
        "99\u2013100, 1\u20132",
        "deletion: ACGT \u2192 \u2014",
        "Wrap (misc_feature)",
        "24 of 30, 80% (20 forward, 4 reverse)",
        "\u2014",
      ],
    ]);
    const button = (name: string) =>
      Array.from(root.querySelectorAll("button")).find((b) => b.textContent === name);
    button("Read depth")?.click();
    expect(root.querySelector("path.depth")).toBeNull();
    button("Differences (2)")?.click();
    expect(root.querySelector('[aria-label="Differences"]')).toBeNull();
    button("Linear")?.click();
    button("Read depth")?.click();
    expect(root.querySelectorAll("path.depth")).toHaveLength(1);
    expect(root.querySelector("p")?.textContent).toContain("drawn as a line cut at base 1");
    const dl = root.querySelector("dl")?.textContent ?? "";
    expect(dl).toContain("Read depth fromSynthetic reads");
  });

  it("draws a feature with no strand, or both, without an arrowhead, and says so", () => {
    const map = document.createElement("iki-plasmid-map");
    map.data = {
      name: "Synthetic circle",
      length: 100,
      topology: "circular" as const,
      parts: {
        items: [
          { start: 10, end: 30, name: "Unstranded", type: "misc_feature", strand: "none" as const },
          { start: 50, end: 70, name: "Split", type: "CDS", strand: "mixed" as const },
          { start: 80, end: 90, name: "Ahead", type: "CDS", strand: "forward" as const },
        ],
        own: true,
      },
      notes: ["The consensus's bases 101 to 200 of its 200 repeat bases already placed."],
    };
    document.body.append(map);
    const root = map.shadowRoot;
    assert(root);
    const said = root.querySelector("p")?.textContent ?? "";
    expect(said).toContain("Unstranded, 10 to 30 on neither strand");
    expect(said).toContain("Split, 50 to 70 on both strands");
    expect(said).toContain("Ahead, 80 to 90 on the forward strand");
    // One arrowhead, the forward feature's: the others' head paths are empty.
    const heads = Array.from(root.querySelectorAll("svg path:not(.arc):not(.depth)")).filter(
      (path) => (path.getAttribute("d") ?? "").endsWith("Z"),
    );
    expect(heads).toHaveLength(1);
    const strands = Array.from(
      root.querySelectorAll('[aria-label="Features"] tbody tr'),
      (row) => row.children[3]?.textContent,
    );
    expect(strands).toEqual(["neither", "both", "forward"]);
    expect(root.querySelector("p.note")?.textContent).toBe(
      "The consensus's bases 101 to 200 of its 200 repeat bases already placed.",
    );
  });

  it("says a long change's true size, not its recorded first bases'", () => {
    // The record keeps a change's first 200 bases of each side and the full lengths.
    const repeat = (bases: string, n: number) => bases.repeat(n).slice(0, n);
    const map = document.createElement("iki-plasmid-map");
    map.data = {
      name: "Synthetic circle",
      length: 2000,
      topology: "circular" as const,
      differences: {
        items: [
          {
            start: 101,
            end: 400,
            hgvs: "o.101_400del",
            kind: "deletion" as const,
            reference: repeat("ACGT", 200),
            observed: "",
            length_change: -300,
            reference_length: 300,
            observed_length: 0,
          },
          {
            start: 1001,
            end: 1250,
            hgvs: "o.1001_1250delins[X:o.1001_1300]",
            kind: "deletion_insertion" as const,
            reference: repeat("ACGT", 200),
            observed: repeat("TTGCA", 200),
            length_change: 50,
            reference_length: 250,
            observed_length: 300,
          },
          {
            start: 1500,
            end: 1501,
            hgvs: "o.1500_1501ins[X:o.1551_1750]",
            kind: "insertion" as const,
            reference: "",
            observed: repeat("GGC", 200),
            length_change: 200,
            reference_length: 0,
            observed_length: 200,
          },
          {
            start: 1600,
            end: 1611,
            hgvs: "o.1600_1611delinsAC",
            kind: "deletion_insertion" as const,
            reference: "ACGTACGTACGT",
            observed: "AC",
            length_change: -10,
            reference_length: 12,
            observed_length: 2,
          },
        ],
      },
    };
    document.body.append(map);
    const changes = Array.from(
      map.shadowRoot?.querySelectorAll('[aria-label="Differences"] tbody tr') ?? [],
      (row) => row.children[2]?.textContent,
    );
    expect(changes).toEqual([
      "deletion: 300 bp \u2192 \u2014",
      "deletion-insertion: 250 bp \u2192 300 bp",
      "insertion: \u2014 \u2192 200 bp",
      "deletion-insertion: ACGTACGTACGT \u2192 AC",
    ]);
  });

  it("reports measured gaps when their coordinates are missing", () => {
    for (const low of [undefined, []]) {
      const map = document.createElement("iki-plasmid-map");
      map.data = { ...evidence, coverage: { ...evidence.coverage, low } };
      document.body.append(map);
      expect(map.shadowRoot?.querySelector("p")?.textContent).not.toContain("every base at");
      expect(
        map.shadowRoot?.querySelector('[aria-label="Read depth"] table')?.textContent,
      ).toContain("interval details unavailable");
    }
  });

  it("identifies partial interval coordinates in its description and table", () => {
    const map = document.createElement("iki-plasmid-map");
    map.data = { ...evidence, coverage: { ...evidence.coverage, low_count: 202 } };
    document.body.append(map);
    for (const selector of ["p", '[aria-label="Read depth"] table']) {
      expect(map.shadowRoot?.querySelector(selector)?.textContent).toContain(
        "showing 1 of 202 intervals; see coverage.tsv for all",
      );
    }
  });

  it("says when every base is deep enough and nothing differs", () => {
    const map = document.createElement("iki-plasmid-map");
    map.data = {
      ...evidence,
      parts: { items: [], own: true },
      coverage: {
        ...evidence.coverage,
        mean: [30, 15],
        min: [25, 10],
        meanDepth: 22.5,
        leastDepth: 10,
        low: [],
      },
      differences: { items: [] },
    };
    document.body.append(map);
    const said = map.shadowRoot?.querySelector("p")?.textContent ?? "";
    expect(said).toContain("every base at the minimum depth of 10 or more");
    expect(said).toContain("with no differences from the record");
    expect(said).toContain("with no features of its own");
  });
});

describe("map thumbnail", () => {
  it("draws a whole circle and both sides of a wrapping feature, then opens by DOM event", () => {
    const map = document.createElement("iki-plasmid-thumbnail");
    map.data = {
      title: "Synthetic circle",
      length: 40,
      alphabet: "dna",
      circular: true,
      features: [
        { type: "gene", name: "Whole", start: 1, end: 40 },
        { type: "misc_feature", name: "Wrap", start: 38, end: 3 },
      ],
      openLabel: "Open map",
    };
    document.body.append(map);
    expect(map.querySelector('[role="img"]')?.getAttribute("aria-label")).toBe(
      "Circular map of Synthetic circle, 40 bp: Whole, Wrap",
    );
    expect(map.querySelectorAll("path")).toHaveLength(3);
    let opened = false;
    map.addEventListener("thumbnail-open", (event) => {
      opened = event.bubbles && event.composed;
    });
    map.querySelector("button")?.click();
    expect(opened).toBe(true);
  });
});
