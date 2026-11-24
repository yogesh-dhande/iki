import { describe, expect, it } from "vitest";

import { featureName, kindOf, shortHgvs } from "./hgvs.ts";

describe("differences as a table shows them", () => {
  it("shortens a long run of inserted bases, and says how many", () => {
    const long = `o.419_420ins${"ACGT".repeat(21)}`;
    expect(shortHgvs(long)).toEqual({ text: "o.419_420insACGTACGTAC…", bases: 84 });
    expect(shortHgvs("o.419_420insACGT")).toEqual({ text: "o.419_420insACGT", bases: null });
    expect(shortHgvs("o.887del")).toEqual({ text: "o.887del", bases: null });
  });

  it("names each kind, and an insertion's length", () => {
    expect(kindOf("insertion", 84)).toBe("insertion, 84 bp");
    expect(kindOf("insertion", 1)).toBe("insertion");
    expect(kindOf("deletion_insertion", 0)).toBe("deletion-insertion");
    expect(featureName("ori (rep_origin)")).toBe("ori");
  });
});
