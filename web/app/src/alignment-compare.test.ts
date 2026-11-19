import { describe, expect, it } from "vitest";

import { call, compared, MAX_COMPARED, possibleOf } from "./alignment-compare";

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
    });
  });

  it("counts a residue where the reference has a gap as a difference, and X as unknown", () => {
    const reference = row("MK-LV");
    const rows = [reference, row("MKWLX")];

    expect(compared(rows, reference, "protein")).toEqual({
      residues: 4,
      covered: 3,
      differing: 1,
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
    ).toEqual({ residues: 41, covered: 40, differing: 1 });
    expect(compared([reference, row(read)], reference, "protein")).toEqual({
      residues: 41,
      covered: 39,
      differing: 0,
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

  it("says nothing of an alignment too large to compare on the page", () => {
    const long = "A".repeat(MAX_COMPARED / 2 + 1);
    const reference = row(long);

    expect(compared([reference, row(long)], reference, "nucleotide")).toBeNull();
  });
});
