/** The sequence viewer's layout: rows, lanes, and features that wrap the origin. */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  AT_START,
  caretRow,
  columnOf,
  complement,
  extremes,
  featureLength,
  findMotif,
  gcContent,
  kindOf,
  lanesPerRow,
  lengthText,
  locationText,
  meltingTemperature,
  moved,
  piecesIn,
  placeAt,
  readFeatures,
  readMotif,
  readTrack,
  residuesOf,
  reverseComplement,
  selected,
  stepTo,
  stretchLength,
  trackScale,
  type Caret,
  type Feature,
} from "./sequence";

/** A file of the repository's, from web/app (as pnpm runs the tests) or from its root. */
function repositoryFile(path: string): string {
  const here = join(process.cwd(), "..", "..", path);
  return readFileSync(existsSync(here) ? here : path, "utf8");
}

/** pUC19c's 2,686 bases (GenBank L09137.2), as the server's tests read them. */
const PUC19C = repositoryFile("server/tests/data/L09137.2.fasta").split("\n").slice(1).join("");

const PUC19 = [
  { type: "CDS", name: "bla", start: 1626, end: 2486, strand: "reverse" },
  { type: "promoter", name: "AmpR promoter", start: 2487, end: 2591, strand: "reverse" },
  { type: "rep_origin", name: "ori", start: 867, end: 1455, strand: "reverse" },
  { type: "CDS", name: "lacZα", start: 146, end: 469, strand: "reverse" },
  { type: "misc_feature", name: "MCS", start: 397, end: 453 },
];

describe("features", () => {
  it("are read as far as they can be drawn, and laned where they overlap", () => {
    const read = readFeatures([...PUC19, { type: "CDS", start: 0, end: 5 }, "junk"], 2686);
    expect(read.map((f) => [f.name, f.lane])).toEqual([
      ["bla", 0],
      ["AmpR promoter", 0],
      ["ori", 0],
      ["lacZα", 0],
      ["MCS", 1],
    ]);
    expect(read[4]?.strand).toBe("none");
    expect(readFeatures({ not: "a list" }, 10)).toEqual([]);
  });

  it("wrap the origin of a circular sequence when they end before they start", () => {
    const [wrapped] = readFeatures([{ type: "CDS", start: 2600, end: 120 }], 2686) as [Feature];
    expect(featureLength(wrapped, 2686)).toBe(87 + 120);
    expect(locationText(wrapped)).toBe("2600..120 (across the origin)");
    expect(piecesIn([wrapped], 1, 2686)).toMatchObject([{ column: 0, width: 65, arrow: null }]);
    expect(piecesIn([wrapped], 2641, 2686)).toMatchObject([{ shape: "part", arrow: null }]);
  });

  it("are drawn in their row's columns, with a gap after every ten residues", () => {
    expect(columnOf(0)).toBe(0);
    expect(columnOf(9)).toBe(9);
    expect(columnOf(10)).toBe(11);
    const [mcs] = readFeatures([{ type: "misc_feature", start: 397, end: 453 }], 2686) as [Feature];
    // Row 361..420 holds 397..420: offsets 36..59.
    expect(piecesIn([mcs], 361, 2686)).toEqual([
      { feature: mcs, column: 39, width: 26, shape: "part", arrow: null },
    ]);
  });

  it("cover only their parts: a join's, not what's between them, and a site none", () => {
    // INSDC: join(2..4,10..12) is six bases; 5^6 is the site between 5 and 6.
    const [joined, site] = readFeatures(
      [
        {
          type: "CDS",
          start: 2,
          end: 12,
          strand: "forward",
          parts: [
            { start: 2, end: 4, strand: "forward" },
            { start: 10, end: 12, strand: "forward" },
          ],
        },
        {
          type: "misc_feature",
          start: 5,
          end: 6,
          parts: [{ start: 5, end: 6, between: true }],
        },
      ],
      20,
    ) as [Feature, Feature];
    expect(featureLength(joined, 20)).toBe(6);
    expect(locationText(joined)).toBe("join(2..4,10..12)");
    expect(piecesIn([joined], 1, 20)).toEqual([
      { feature: joined, column: 4, width: 5, shape: "joining", arrow: null },
      { feature: joined, column: 1, width: 3, shape: "part", arrow: null },
      { feature: joined, column: 9, width: 4, shape: "part", arrow: "forward" },
    ]);
    expect(featureLength(site, 20)).toBe(0);
    expect(locationText(site)).toBe("5^6");
    expect(piecesIn([site], 1, 20)).toMatchObject([{ column: 4, shape: "site", arrow: null }]);
  });

  it("say when their parts are an order, not a join, and when their ends are partial", () => {
    // INSDC: order(2..4,10..12) says only that they're found in that order; <2..>9 goes
    // on past both ends, so 8 bases is only what's annotated of it.
    const number = new Intl.NumberFormat("en");
    const [ordered, partial, reverse, single] = readFeatures(
      [
        {
          type: "misc_feature",
          start: 2,
          end: 12,
          strand: "forward",
          operator: "order",
          parts: [
            { start: 2, end: 4, strand: "forward" },
            { start: 10, end: 12, strand: "forward" },
          ],
        },
        {
          type: "CDS",
          start: 2,
          end: 9,
          strand: "forward",
          parts: [{ start: 2, end: 9, strand: "forward", partial_start: true, partial_end: true }],
        },
        // SV40's VP1 from its other strand: complement(2651..>3745).
        {
          type: "CDS",
          start: 2651,
          end: 3745,
          strand: "reverse",
          parts: [{ start: 2651, end: 3745, strand: "reverse", partial_end: true }],
        },
        {
          type: "misc_feature",
          start: 7,
          end: 7,
          strand: "forward",
          parts: [{ start: 7, end: 7, strand: "forward", partial_start: true }],
        },
      ],
      5243,
    ) as [Feature, Feature, Feature, Feature];
    expect(locationText(ordered)).toBe("order(2..4,10..12)");
    expect(lengthText(ordered, 5243, number)).toBe("6");
    expect(locationText(partial)).toBe("<2..>9");
    expect(lengthText(partial, 5243, number)).toBe("at least 8");
    expect(locationText(reverse)).toBe("complement(2651..>3745)");
    expect(lengthText(reverse, 5243, number)).toBe("at least 1,095");
    expect(locationText(single)).toBe("<7");
  });

  it("cross the origin only where their parts do, the arrow at the last one's end", () => {
    // complement(join(90..100,1..5)) on a 100-base circle: 16 bases, read 5 to 1, then
    // 100 to 90, so it ends at 90.
    const [reverse] = readFeatures(
      [
        {
          type: "CDS",
          start: 90,
          end: 5,
          strand: "reverse",
          parts: [
            { start: 1, end: 5, strand: "reverse" },
            { start: 90, end: 100, strand: "reverse" },
          ],
        },
      ],
      100,
    ) as [Feature];
    expect(featureLength(reverse, 100)).toBe(16);
    expect(locationText(reverse)).toBe("complement(join(90..100,1..5)) (across the origin)");
    const pieces = piecesIn([reverse], 61, 100);
    expect(pieces.filter((p) => p.shape === "joining")).toEqual([]);
    expect(pieces.find((p) => p.arrow)).toMatchObject({ arrow: "reverse", column: 31 });
    expect(piecesIn([reverse], 1, 100).every((p) => p.arrow === null)).toBe(true);
  });

  it("say when their parts lie on both strands", () => {
    const [mixed] = readFeatures(
      [
        {
          type: "mRNA",
          start: 1,
          end: 20,
          strand: "mixed",
          parts: [
            { start: 1, end: 5, strand: "forward" },
            { start: 10, end: 20, strand: "reverse" },
          ],
        },
      ],
      30,
    ) as [Feature];
    expect(mixed.strand).toBe("mixed");
    expect(locationText(mixed)).toBe("join(1..5,complement(10..20))");
    // Parts that don't lie on the sequence leave the feature out.
    expect(
      readFeatures([{ type: "CDS", start: 1, end: 5, parts: [{ start: 1, end: 50 }] }], 30),
    ).toEqual([]);
  });

  it("say how many lanes each row needs", () => {
    const read = readFeatures(PUC19, 2686);
    const lanes = lanesPerRow(read, 2686);
    expect(lanes).toHaveLength(45);
    expect(lanes[0]).toBe(0);
    expect(lanes[6]).toBe(2); // 361..420: lacZα and the MCS
  });

  it("look like their kind", () => {
    expect(kindOf("CDS")).toBe("coding");
    expect(kindOf("rep_origin")).toBe("origin");
    expect(kindOf("misc_feature")).toBe("other");
  });
});

describe("tracks", () => {
  it("are read only when they hold one number, or null, per residue", () => {
    expect(readTrack([null, 1.5, -2], 3)).toEqual([null, 1.5, -2]);
    expect(readTrack([1, 2], 3)).toBeNull();
    expect(readTrack([1, "2", 3], 3)).toBeNull();
    expect(readTrack([1, Number.NaN, 3], 3)).toBeNull();
    expect(readTrack("1,2,3", 3)).toBeNull();
  });

  it("scale from their field's bounds, or else their values, with zero in them", () => {
    expect(trackScale([null, 1.5, -2], -4.5, 4.5)).toEqual([-4.5, 4.5]);
    expect(trackScale([null, 1.5, -2], null, null)).toEqual([-2, 1.5]);
    expect(trackScale([3, 5], null, null)).toEqual([0, 5]);
    expect(trackScale([null, null], null, null)).toEqual([0, 1]);
  });

  it("say where their highest and lowest values are", () => {
    expect(extremes([null, 1.5, -2, 1.5])).toEqual({ highest: [1.5, 2], lowest: [-2, 3] });
    expect(extremes([null, null])).toBeNull();
  });
});

describe("the two strands", () => {
  it("pair base by base, each IUPAC code with its complement's", () => {
    expect(complement("ACGTRYKMBVDHSWN")).toBe("TGCAYRMKVBHDSWN");
    expect(complement("acgtn")).toBe("tgcan");
    expect(complement("AUGC", "rna")).toBe("UACG");
    expect(complement("AC-GT")).toBe("TG-CA");
  });

  it("read as the other strand 5′ to 3′: what binds them", () => {
    // pUC19's M13/pUC forward primer site, and the reverse primer (NEB S1224, S1201).
    expect(reverseComplement("GTAAAACGACGGCCAGT")).toBe("ACTGGCCGTCGTTTTAC");
    expect(reverseComplement("GAATTC")).toBe("GAATTC"); // EcoRI's site is its own
    expect(reverseComplement("AUGGCC", "rna")).toBe("GGCCAU");
  });
});

describe("what a stretch of bases is", () => {
  it("has the GC content the DNA plugin's compute_composition records", () => {
    // plugins/dna/scripts/sequences.py's gc_content of the same bases.
    expect(PUC19C).toHaveLength(2686);
    expect(gcContent(PUC19C)).toBe(50.63);
    expect(gcContent("GTAAAACGACGGCCAGT")).toBe(52.94);
    // N says nothing of pairing; S is G or C, W is A or T.
    expect(gcContent("ACGN")).toBe(66.67);
    expect(gcContent("SSWW")).toBe(50);
    expect(gcContent("NNRY")).toBeNull();
  });

  it("melts a selected stretch in sodium alone: SantaLucia 1998, 50 mM Na+, 50 nM strands", () => {
    // Biopython's Tm_NN with DNA_NN3, Na=50, saltcorr=5, dnac1=dnac2=25, of the same
    // bases. Not a primer's own melting_temperature, which is quoted in a PCR buffer and
    // reads several degrees higher (ADR 0201).
    expect(meltingTemperature("GTAAAACGACGGCCAGT")).toBe(49.2); // M13/pUC forward
    expect(meltingTemperature("CAGGAAACAGCTATGAC")).toBe(43.6); // M13/pUC reverse
    expect(meltingTemperature(PUC19C.slice(0, 24))).toBe(64.2);
    expect(meltingTemperature("gtaaaacgacggccagt")).toBe(49.2);
    // A strand that pairs with itself, at CT rather than CT/4, with the symmetry term.
    expect(meltingTemperature("GAATTC")).toBe(-20.4);
    expect(meltingTemperature("GTAAAACGNCGGCCAGT")).toBeNull();
    expect(meltingTemperature("A")).toBeNull();
  });
});

describe("the caret and the selection", () => {
  const plain = { shiftKey: false, ctrlKey: false, metaKey: false, altKey: false };
  const key = (name: string, more: Partial<typeof plain> = {}) => ({
    key: name,
    ...plain,
    ...more,
  });

  it("selects from the anchor to the caret, either way, or round a circle's origin", () => {
    expect(selected(AT_START)).toBeNull();
    expect(selected({ anchor: 395, caret: 401, across: false })).toEqual([396, 401]);
    expect(selected({ anchor: 401, caret: 395, across: false })).toEqual([396, 401]);
    expect(selected({ anchor: 2680, caret: 4, across: true })).toEqual([2681, 4]);
    expect(stretchLength(2681, 4, 2686)).toBe(10);
    expect(residuesOf("ABCDEFGH", 7, 2)).toBe("GHAB");
  });

  it("moves as a text field's caret does, and extends the selection with Shift", () => {
    let at: Caret = AT_START;
    const press = (name: string, more: Partial<typeof plain> = {}) => {
      at = moved(at, key(name, more), 2686, 10) ?? at;
      return at;
    };
    expect(press("ArrowRight").caret).toBe(1);
    expect(press("ArrowLeft").caret).toBe(0);
    expect(press("ArrowLeft").caret).toBe(0); // no further than the start
    expect(press("ArrowDown").caret).toBe(60);
    // The end of a row is the next one's start: it shows there, unless End put it.
    expect(caretRow(at, 2686)).toBe(1);
    expect(press("End")).toMatchObject({ caret: 120, atEnd: true });
    expect(caretRow(at, 2686)).toBe(1);
    expect(press("Home").caret).toBe(60);
    expect(press("ArrowRight", { shiftKey: true })).toMatchObject({ anchor: 60, caret: 61 });
    expect(press("End", { shiftKey: true })).toMatchObject({ anchor: 60, caret: 120 });
    expect(selected(at)).toEqual([61, 120]);
    // Without Shift, Left goes to the selection's start and Right to its end.
    expect(press("ArrowLeft")).toMatchObject({ anchor: 60, caret: 60 });
    expect(press("ArrowRight", { ctrlKey: true }).caret).toBe(70);
    expect(press("ArrowRight", { altKey: true }).caret).toBe(80);
    expect(press("ArrowLeft", { altKey: true }).caret).toBe(70);
    expect(press("PageDown").caret).toBe(670);
    expect(press("End", { ctrlKey: true }).caret).toBe(2686);
    expect(press("Home", { metaKey: true }).caret).toBe(0);
    expect(press("ArrowRight", { metaKey: true })).toMatchObject({ caret: 60, atEnd: true });
    expect(press("a", { ctrlKey: true })).toMatchObject({ anchor: 0, caret: 2686 });
    expect(moved(at, key("x"), 2686, 10)).toBeNull();
  });

  it("goes where a pointer is, between the bases nearest it", () => {
    // Row 61..120: columns 0..9 are 61..70, 10 is the gap after them, 11 is 71's.
    expect(placeAt(0, 61, 2686)).toBe(60);
    expect(placeAt(0.6, 61, 2686)).toBe(61);
    expect(placeAt(10.4, 61, 2686)).toBe(70);
    expect(placeAt(11.2, 61, 2686)).toBe(70);
    expect(placeAt(70, 61, 2686)).toBe(120);
    // The last row is as long as what's left of it.
    expect(placeAt(50, 2641, 2686)).toBe(2686);
  });
});

describe("finding a motif", () => {
  const dna = { alphabet: "dna" as const, bothStrands: true, circular: true };

  it("finds pUC19's polylinker sites where the cloning tests cut them", () => {
    // server/tests/test_plasmid_cloning.py: EcoRI cuts after base 396 (G^AATTC) and
    // HindIII after base 447 (A^AGCTT). Each site is its own reverse complement.
    expect(findMotif(PUC19C, "GAATTC", dna).matches).toEqual([
      { start: 396, end: 401, strand: "both" },
    ]);
    expect(findMotif(PUC19C, "AAGCTT", dna).matches).toEqual([
      { start: 447, end: 452, strand: "both" },
    ]);
  });

  it("reads IUPAC codes in the motif, on both strands, as Biopython's nt_search does", () => {
    // BstYI's RGATCY: Bio.SeqUtils.nt_search finds it at these seven places on each strand.
    expect(findMotif(PUC19C, "RGATCY", dna).matches.map((m) => [m.start, m.strand])).toEqual(
      [417, 1447, 1458, 1544, 1556, 2324, 2341].map((at) => [at, "both"]),
    );
    expect(findMotif(PUC19C, "GCGC", dna).matches).toHaveLength(17);
    // The M13/pUC primers bind one strand each: the forward one's bases are on the forward
    // strand, the reverse one's reverse complement is.
    expect(findMotif(PUC19C, "GTAAAACGACGGCCAGT", dna).matches).toEqual([
      { start: 379, end: 395, strand: "forward" },
    ]);
    expect(findMotif(PUC19C, "CAGGAAACAGCTATGAC", dna).matches).toEqual([
      { start: 465, end: 481, strand: "reverse" },
    ]);
    expect(findMotif(PUC19C, "CAGGAAACAGCTATGAC", { ...dna, bothStrands: false }).matches).toEqual(
      [],
    );
  });

  it("reads across a circle's origin, and never across a line's ends", () => {
    // pUC19c's last three bases and its first three.
    const across = PUC19C.slice(-3) + PUC19C.slice(0, 3);
    expect(across).toBe("GTCTCG");
    expect(findMotif(PUC19C, across, dna).matches).toEqual([
      { start: 1765, end: 1770, strand: "reverse" },
      { start: 2684, end: 3, strand: "forward" },
    ]);
    expect(findMotif(PUC19C, across, { ...dna, circular: false }).matches).toEqual([
      { start: 1765, end: 1770, strand: "reverse" },
    ]);
  });

  it("matches an ambiguous base only with a code that allows all it could be", () => {
    // The motif's N matches anything; its A doesn't match an N, which only might be one.
    expect(findMotif("ACNGT", "CNG", dna).matches).toHaveLength(1);
    expect(findMotif("ACNGT", "CAG", dna).matches).toHaveLength(0);
    expect(findMotif("ACRGT", "CRG", { ...dna, bothStrands: false }).matches).toHaveLength(1);
    expect(findMotif("ACRGT", "CNG", { ...dna, bothStrands: false }).matches).toHaveLength(1);
    // RNA's U is T.
    expect(findMotif("GAAUUC", "GAATTC", { ...dna, alphabet: "rna" }).matches).toHaveLength(1);
  });

  it("finds a protein's residues, X standing for any and B for D or N", () => {
    const protein = { alphabet: "protein" as const, bothStrands: true, circular: false };
    // Human haemoglobin alpha's first residues (UniProt P69905).
    const hba = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMF";
    expect(findMotif(hba, "GXVG", protein).matches).toEqual([
      { start: 16, end: 19, strand: "forward" },
    ]);
    expect(findMotif(hba, "BV", protein).matches.map((m) => m.start)).toEqual([10]);
  });

  it("reads what was typed, or says why not", () => {
    expect(readMotif(" gaa ttc 12", "dna")).toEqual({ motif: "GAATTC" });
    expect(readMotif("GAZTTC", "dna")).toEqual({ error: "Z isn't an IUPAC nucleotide code." });
    expect(readMotif("MKO1", "protein")).toEqual({ motif: "MKO" });
    expect(readMotif("MK@", "protein")).toEqual({
      error: "@ isn't an amino acid's one-letter code.",
    });
    expect(readMotif("A".repeat(201), "dna")).toEqual({ error: "That's 201 long: at most 200." });
    expect(readMotif("  ", "dna")).toBeNull();
  });

  it("steps from the caret to the next match, or back, round past the ends", () => {
    const { matches } = findMotif(PUC19C, "RGATCY", dna);
    expect(stepTo(matches, 0, 1)).toEqual({ index: 0, wrapped: false });
    expect(stepTo(matches, 1500, 1)).toEqual({ index: 3, wrapped: false });
    expect(stepTo(matches, 2400, 1)).toEqual({ index: 0, wrapped: true });
    expect(stepTo(matches, 1500, -1)).toEqual({ index: 2, wrapped: false });
    expect(stepTo(matches, 100, -1)).toEqual({ index: 6, wrapped: true });
    expect(stepTo([], 0, 1)).toBeNull();
  });
});
