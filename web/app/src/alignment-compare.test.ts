import { describe, expect, it } from "vitest";

import { call, compared, MAX_COMPARED, possibleOf, stepped } from "./alignment-compare";

const row = (aligned: string) => ({ aligned });

describe("compared", () => {
  it("counts what reads cover of the reference and where they differ, within their spans", () => {
    const reference = row("ACGTACGTAC");
    const rows = [
      reference,
      // Covers 1-6, a C for the G at 3 and a deletion at 5.
      row("ACCT-C----"),
      // Covers 8 and 10: an N at 9 says nothing, so it neither covers nor differs.
      row("-------TNC"),
    ];

    expect(compared(rows, reference, "nucleotide")).toEqual({
      residues: 10,
      // 7 and 9 aren't covered.
      covered: 8,
      differing: 2,
      // From 0: the C at 3 and the deletion at 5.
      columns: [2, 4],
    });
  });

  it("counts a residue where the reference has a gap as a difference, and X as unknown", () => {
    const reference = row("MK-LV");
    const rows = [reference, row("MKWLX")];

    expect(compared(rows, reference, "protein")).toEqual({
      residues: 4,
      covered: 3,
      differing: 1,
      columns: [2],
    });
  });

  it("takes a place with no observation for none, not for a deletion", () => {
    // A 24-base read across an 80-base circle's origin covers 1-12 and 69-80: the action
    // writes `.` where it has no observation, between them.
    const circle = "ACGT".repeat(20);
    const reference = row(circle);
    const read = row(circle.slice(0, 12) + ".".repeat(56) + circle.slice(68));

    expect(compared([reference, read], reference, "nucleotide")).toEqual({
      residues: 80,
      covered: 24,
      differing: 0,
      columns: [],
    });
  });

  it("compares ambiguity codes as the sets they stand for, the same either way round", () => {
    // IUBMB: N is any base; R is A or G, Y is C or T.
    expect(call("A", "N", "nucleotide")).toBe("same");
    expect(call("N", "A", "nucleotide")).toBe("unresolved");
    expect(call("Y", "R", "nucleotide")).toBe("differs");
    // IUPAC-IUB: B is D or N.
    expect(call("D", "B", "protein")).toBe("same");
    expect(call("B", "D", "protein")).toBe("unresolved");
    expect(call("B", "A", "protein")).toBe("differs");
    const a = compared([row("ANA"), row("AAA")], row("ANA"), "nucleotide");
    const b = compared([row("AAA"), row("ANA")], row("AAA"), "nucleotide");
    expect([a?.differing, b?.differing]).toEqual([0, 0]);
    expect(compared([row("B"), row("D")], row("B"), "protein")?.differing).toBe(0);
  });

  it("compares an X as what its header says it could be", () => {
    // Haemoglobin alpha's first 41 residues (UniProt P69905), and a read whose codon at Ala20
    // is AAN: AAA, AAC, AAG or AAT, Lys or Asn by NCBI's standard code, neither of them Ala,
    // so it differs there, as align_reads reports (A20X). Its NNN at Lys17 could be anything,
    // so it says nothing there. A row without the header's list keeps X as anything.
    const hba = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTK";
    const reference = row(hba);
    const read = `${hba.slice(0, 16)}X${hba.slice(17, 19)}X${hba.slice(20)}`;
    const header = "read_1 forward frame 1, bases 1–123 of 123, from read_1.ab1 possible=20:KN";
    expect(possibleOf(header)).toEqual(new Map([[19, "KN"]]));
    expect(
      compared([reference, { aligned: read, description: header }], reference, "protein"),
    ).toEqual({ residues: 41, covered: 40, differing: 1, columns: [19] });
    expect(compared([reference, row(read)], reference, "protein")).toEqual({
      residues: 41,
      covered: 39,
      differing: 0,
      columns: [],
    });
    // A stop is one of what a codon can be: TGN is Cys, Trp or a stop.
    expect(possibleOf("r possible=3:*CW,12:FL")).toEqual(
      new Map([
        [2, "*CW"],
        [11, "FL"],
      ]),
    );
    // Only a list that ends the header is one.
    expect(possibleOf("r possible=3:KN from x.ab1").size).toBe(0);
    expect(possibleOf("r from impossible=3:KN").size).toBe(0);
  });

  it("takes a translated codon that can only be what an ambiguous reference allows as covering it", () => {
    // Haemoglobin alpha's first 41 residues (UniProt P69905) with Asx (B, Asp or Asn) at
    // 21, and a read whose codon there is RAY: AAC, AAT, GAC or GAT, Asn or Asp by NCBI's
    // standard code, so align_reads writes B and calls it the same, covering all 41. An X
    // at 21 read as AAN (Lys or Asn) is likewise within what X allows. A bare X, a codon
    // such as NNN that could also be a stop, still says nothing there.
    const hba = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTK";
    const asx = row(`${hba.slice(0, 20)}B${hba.slice(21)}`);
    expect(compared([asx, row(asx.aligned)], asx, "protein")).toEqual({
      residues: 41,
      covered: 41,
      differing: 0,
      columns: [],
    });
    const unknown = row(`${hba.slice(0, 20)}X${hba.slice(21)}`);
    const aan = { aligned: unknown.aligned, description: "r forward frame 1 possible=21:KN" };
    expect(compared([unknown, aan], unknown, "protein")).toEqual({
      residues: 41,
      covered: 41,
      differing: 0,
      columns: [],
    });
    expect(compared([unknown, row(unknown.aligned)], unknown, "protein")).toEqual({
      residues: 41,
      covered: 40,
      differing: 0,
      columns: [],
    });
    // A nucleotide's ambiguity code says nothing of the same code, as align_reads in the
    // DNA plugin has it.
    expect(call("R", "R", "nucleotide")).toBe("unresolved");
  });

  it("lists the columns it counts as differing, in order, each once", () => {
    // Two reads differing from the reference at the same column count it once.
    const reference = row("ACGTACGTAC");
    const rows = [reference, row("ACGAAC----"), row("----ACGTCC"), row("-CGAAC----")];
    const found = compared(rows, reference, "nucleotide");
    expect(found?.columns).toEqual([3, 8]);
    expect(found?.differing).toBe(found?.columns.length);
  });

  it("steps to the next or previous differing column, round past either end", () => {
    const columns = [3, 8, 429];
    expect(stepped(columns, 0, 1)).toEqual({ index: 0, wrapped: false });
    expect(stepped(columns, 3, 1)).toEqual({ index: 1, wrapped: false });
    expect(stepped(columns, 5, 1)).toEqual({ index: 1, wrapped: false });
    expect(stepped(columns, 429, 1)).toEqual({ index: 0, wrapped: true });
    expect(stepped(columns, 900, 1)).toEqual({ index: 0, wrapped: true });
    expect(stepped(columns, 429, -1)).toEqual({ index: 1, wrapped: false });
    expect(stepped(columns, 9, -1)).toEqual({ index: 1, wrapped: false });
    expect(stepped(columns, 3, -1)).toEqual({ index: 2, wrapped: true });
    expect(stepped(columns, 0, -1)).toEqual({ index: 2, wrapped: true });
    // With one, it's where it is either way.
    expect(stepped([429], 429, 1)).toEqual({ index: 0, wrapped: true });
    expect(stepped([], 0, 1)).toBeNull();
  });

  it("says nothing of an alignment too large to compare on the page", () => {
    const long = "A".repeat(MAX_COMPARED / 2 + 1);
    const reference = row(long);

    expect(compared([reference, row(long)], reference, "nucleotide")).toBeNull();
  });
});
