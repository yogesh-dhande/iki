import { expect, it } from "vitest";

import { proteinOf, translateFrame } from "./translation.js";

it("translates whole codons with one-based coordinates in the indicated frame", () => {
  // A synthetic two-codon example from NCBI genetic code 1 plus a terminal stop.
  const codons = translateFrame("ATGGCTTAA", 1, 1);
  expect(codons.map(({ residue, bases }) => [residue, bases])).toEqual([
    ["M", [1, 2, 3]],
    ["A", [4, 5, 6]],
    ["*", [7, 8, 9]],
  ]);
  expect(proteinOf(codons)).toBe("MA");
  expect(translateFrame("AT", 1, 1)).toEqual([]);
});
