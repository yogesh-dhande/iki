import { describe, expect, it } from "vitest";
import { openReadingFrames } from "./open-reading-frames.js";

describe("ORF annotations on synthetic sequence fragments", () => {
  // Table 1 and table 2, NCBI genetic codes:
  // https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi .
  it("keeps the outer complete ORF ending at each stop in its frame", () => {
    expect(openReadingFrames("ATGATGNNNTAA", { minimumCodons: 1 })).toEqual([
      {
        start: 1,
        end: 12,
        strand: "forward",
        frame: 1,
        start_codon: "ATG",
        length: 12,
        amino_acids: 3,
        translation: "MMX",
      },
    ]);
    expect(openReadingFrames("TTATTTCAT", { minimumCodons: 1 })).toEqual([
      {
        start: 1,
        end: 9,
        strand: "reverse",
        frame: -1,
        start_codon: "ATG",
        length: 9,
        amino_acids: 2,
        translation: "MK",
      },
    ]);
    expect(openReadingFrames("ATGAAA", { minimumCodons: 1 })).toEqual([]);
    expect(openReadingFrames("ATGTAATAA")).toEqual([]);
  });
  it("reads through a circular origin even when length is not divisible by three", () => {
    const synthetic = "TAACATGAAA";
    expect(openReadingFrames(synthetic, { minimumCodons: 1 })).toEqual([]);
    expect(openReadingFrames(synthetic, { circular: true, minimumCodons: 1 })).toMatchObject([
      {
        start: 5,
        end: 3,
        frame: 2,
        strand: "forward",
        length: 9,
        translation: "MK",
      },
    ]);
  });
  it("uses the requested table's starts and stops", () => {
    expect(
      openReadingFrames("GTGAAAAGA", { minimumCodons: 1, table: 2, alternativeStarts: true }),
    ).toMatchObject([
      {
        start_codon: "GTG",
        translation: "MK",
      },
    ]);
    expect(openReadingFrames("GTGAAAAGA", { minimumCodons: 1, table: 2 })).toEqual([]);
    expect(() => openReadingFrames("ATGTAA", { table: 28 })).toThrow(RangeError);
    expect(() => openReadingFrames("", { minimumCodons: 0 })).toThrow(RangeError);
  });
});
