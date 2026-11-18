import { describe, expect, it } from "vitest";

import { call, compared, MAX_COMPARED } from "./alignment-compare";

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

  it("says nothing of an alignment too large to compare on the page", () => {
    const long = "A".repeat(MAX_COMPARED / 2 + 1);
    const reference = row(long);

    expect(compared([reference, row(long)], reference, "nucleotide")).toBeNull();
  });
});
