/** The sequence viewer's layout: rows, lanes, and features that wrap the origin. */

import { describe, expect, it } from "vitest";

import {
  columnOf,
  extremes,
  featureLength,
  kindOf,
  lanesPerRow,
  locationText,
  piecesIn,
  readFeatures,
  readTrack,
  trackScale,
  type Feature,
} from "./sequence";

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
    expect(piecesIn([wrapped], 1, 2686)).toMatchObject([{ column: 0, width: 65, last: false }]);
    expect(piecesIn([wrapped], 2641, 2686)).toMatchObject([{ first: false, last: false }]);
  });

  it("are drawn in their row's columns, with a gap after every ten residues", () => {
    expect(columnOf(0)).toBe(0);
    expect(columnOf(9)).toBe(9);
    expect(columnOf(10)).toBe(11);
    const [mcs] = readFeatures([{ type: "misc_feature", start: 397, end: 453 }], 2686) as [Feature];
    // Row 361..420 holds 397..420: offsets 36..59.
    expect(piecesIn([mcs], 361, 2686)).toEqual([
      { feature: mcs, column: 39, width: 26, first: true, last: false },
    ]);
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
