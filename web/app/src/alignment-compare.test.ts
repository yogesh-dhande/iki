import { describe, expect, it } from "vitest";

import { compared, MAX_COMPARED } from "./alignment-compare";

const row = (aligned: string) => ({ aligned });

describe("compared", () => {
  it("counts what reads cover of the reference and where they differ, within their spans", () => {
    const reference = row("ACGTACGTAC");
    const rows = [
      reference,
      // Covers 1-6, a C for the G at 3 and a deletion at 5.
      row("ACCT-C----"),
      // Covers 8-10, an unknown base at 9 differs from nothing.
      row("-------TNC"),
    ];

    expect(compared(rows, reference, "nucleotide")).toEqual({
      residues: 10,
      // 7 isn't covered.
      covered: 9,
      differing: 2,
    });
  });

  it("counts a residue where the reference has a gap as a difference, and X as unknown", () => {
    const reference = row("MK-LV");
    const rows = [reference, row("MKWLX")];

    expect(compared(rows, reference, "protein")).toEqual({
      residues: 4,
      covered: 4,
      differing: 1,
    });
  });

  it("says nothing of an alignment too large to compare on the page", () => {
    const long = "A".repeat(MAX_COMPARED / 2 + 1);
    const reference = row(long);

    expect(compared([reference, row(long)], reference, "nucleotide")).toBeNull();
  });
});
